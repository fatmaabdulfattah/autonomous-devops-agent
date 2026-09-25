"""
agents/self_healing_agent/self_healing_agent.py
-------------------------------------------------
Receives a Solution from the Orchestrator (via Knowledge Agent),
AND now subscribes directly to SYNTAX_ERROR_DETECTED from the
MonitoringAgent so syntax/indentation errors are fixed automatically
without waiting for the full Knowledge Agent investigation cycle.

Event flow (syntax errors — fast path)
---------------------------------------
    MonitoringAgent
        └─► SYNTAX_ERROR_DETECTED
                │  data = {
                │    service, incident_id, syntax_errors: [
                │      { file, line, error_type, message, raw_message }
                │    ]
                │  }
                ▼
    SelfHealingAgent._on_syntax_error()
        └─► builds a Solution directly from the event
        └─► calls self.remediate(solution)
        └─► publishes REMEDIATION_COMPLETE / REMEDIATION_FAILED

Event flow (general incidents — normal path)
---------------------------------------------
    Orchestrator / Knowledge Agent
        └─► calls agent.remediate(solution) directly

Workflow inside remediate()
----------------------------
    Step 1 — Guard:    solution must have files with valid paths
    Step 2 — Snapshot: read each file from disk → FileToFix.current_content
    Step 3 — Fixer:    call fix_files() → LLMFixResponse
    Step 4 — Validate: path + new_content present for every entry
    Step 5 — Build:    FileModificationResult list (old vs new content)
    Step 6 — Apply:    write fixes to disk (gated by apply_changes=True)
    Step 7 — Commands: run remediation shell commands in order
    Step 8 — Verify:   LLM verifier confirms the fix actually worked
    Step 9 — Publish:  REMEDIATION_COMPLETE or REMEDIATION_FAILED
"""
import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ── self-healing models ───────────────────────────────────────────────────────
from agents.self_healing_agent.models import (
    LLMFixResponse,
    Solution,
    SelfHealingResult,
    CommandExecutionResult,
    FileModificationResult,
    FileToFix,
    VerificationStatus,
)
from agents.self_healing_agent.llm_fixer import fix_files
from agents.self_healing_agent.llm_verifier import verify_fix

# ── core ──────────────────────────────────────────────────────────────────────
from core.base_agent import BaseAgent, AgentEvent
from core.event_bus import EventBus, Event, EventType
from core.agent_registery import AgentRegistry
from core.models import RemediationStatus

logger = logging.getLogger(__name__)

# Backup location is resolved at runtime from self._project_root — see _apply_to_disk()

# safe SYNTAX_ERROR_DETECTED lookup (added in previous session)
try:
    _SYNTAX_EVENT_TYPE = EventType.SYNTAX_ERROR_DETECTED   # type: ignore[attr-defined]
except AttributeError:
    _SYNTAX_EVENT_TYPE = "monitoring.syntax_error_detected"


class SelfHealingAgent(BaseAgent):
    """
    Autonomous self-healing agent.

    Two entry points:
      1. remediate(solution)          — called by Orchestrator/KnowledgeAgent
      2. _on_syntax_error(event)      — triggered by SYNTAX_ERROR_DETECTED
                                        published by MonitoringAgent

    Example (event-driven, syntax fix):
        # After MonitoringAgent detects IndentationError in main.py:
        #
        #   SYNTAX_ERROR_DETECTED →
        #     { file: "main.py", line: "11",
        #       error_type: "IndentationError",
        #       raw_message: "*** Sorry: IndentationError: ... (main.py, line 11)" }
        #
        # SelfHealingAgent builds a Solution and calls remediate() automatically.

    Example (direct call):
        agent = SelfHealingAgent(bus, registry, apply_changes=True)
        await agent.start()
        result = await agent.remediate(solution)
    """

    def set_llm_provider(self, provider) -> None:
        """Use the provider/model chosen at startup for fixer, verifier and instructor."""
        from agents.self_healing_agent import llm_backend
        llm_backend.set_provider(provider)

    def __init__(
        self,
        event_bus     : EventBus,
        registry      : AgentRegistry,
        apply_changes : bool = False,
        project_root  : str  = ".",
    ):
        super().__init__(
            name      = "self_healing_agent",
            event_bus = event_bus,
            registry  = registry,
        )
        self.apply_changes  = apply_changes
        self._project_root  = Path(project_root).resolve()

        # deduplicate: track incident IDs currently being remediated
        self._active: set[str] = set()

        logger.info(
            "[SelfHealingAgent] Initialized — apply_changes=%s, project_root=%s",
            self.apply_changes, self._project_root,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # BaseAgent lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    async def _setup(self) -> None:
        # Subscribe to the fast-path syntax error event from MonitoringAgent
        self.subscribe(_SYNTAX_EVENT_TYPE, self._on_syntax_error)
        logger.info("[SelfHealingAgent] Subscribed to SYNTAX_ERROR_DETECTED")

    async def _teardown(self) -> None:
        logger.info("[SelfHealingAgent] Stopped")

    async def handle_event(self, event: AgentEvent) -> None:
        pass

    # ──────────────────────────────────────────────────────────────────────────
    # Fast path — SYNTAX_ERROR_DETECTED handler
    # ──────────────────────────────────────────────────────────────────────────

    async def _on_syntax_error(self, event: Event) -> None:
        """
        Called automatically when MonitoringAgent fires SYNTAX_ERROR_DETECTED.

        Converts the event payload directly into a Solution and calls
        remediate() — no Knowledge Agent round-trip needed for syntax errors
        because the broken file and exact line are already known.

        Event payload shape:
            {
              "service"      : "auth-api",
              "incident_id"  : "INC-abc123",
              "severity"     : "high",
              "syntax_errors": [
                  {
                      "file"        : "main.py",
                      "line"        : "11",
                      "error_type"  : "IndentationError",
                      "message"     : "SYNTAX ERROR in main.py:11 — ...",
                      "raw_message" : "*** Sorry: IndentationError: expected an indented block ... (main.py, line 11)",
                  }
              ],
              "summary": "1 syntax error(s) in auth-api — main.py:11"
            }
        """
        data         = event.data
        incident_id  = data.get("incident_id") or event.incident_id or "unknown"
        service      = data.get("service", "unknown")
        syntax_errors: list = data.get("syntax_errors", [])

        if not syntax_errors:
            logger.warning("[SelfHealingAgent] SYNTAX_ERROR_DETECTED with empty syntax_errors — skipping")
            return

        # Deduplicate — don't start a second fix for the same incident
        if incident_id in self._active:
            logger.info("[SelfHealingAgent] Already remediating %s — skipping duplicate event", incident_id)
            return

        logger.error(
            "[SelfHealingAgent] 🔴 SYNTAX_ERROR_DETECTED for %s (%d file(s)) — starting auto-fix",
            service, len(syntax_errors),
        )

        # Build one FileToFix per broken file
        files_to_fix: List[FileToFix] = []
        for err in syntax_errors:
            raw_file = err.get("file", "").strip()
            if not raw_file:
                continue

            # Resolve relative path against project root
            file_path = str(self._resolve_path(raw_file))

            files_to_fix.append(FileToFix(
                path            = file_path,
                line            = int(err.get("line", 0) or 0),
                function        = "<module>",
                exception       = f"{err.get('error_type', 'SyntaxError')}: {err.get('raw_message', err.get('message', ''))}",
                fix_description = (
                    f"Fix {err.get('error_type', 'SyntaxError')} at line {err.get('line', '?')}. "
                    f"Original error: {err.get('raw_message', err.get('message', ''))}"
                ),
            ))

        if not files_to_fix:
            logger.warning("[SelfHealingAgent] No resolvable file paths in syntax_errors — skipping")
            return

        # Build the description of what to fix for the LLM
        error_summary = "; ".join(
            f"{e.get('error_type', 'SyntaxError')} in {e.get('file', '?')} line {e.get('line', '?')}"
            for e in syntax_errors
        )

        solution = Solution(
            incident_id    = incident_id,
            root_cause     = f"Syntax error(s) in {service}: {error_summary}",
            healing_prompt = (
                f"The CI/CD pipeline for '{service}' failed with syntax error(s).\n\n"
                f"Errors detected:\n"
                + "\n".join(
                    f"  • {e.get('error_type', 'SyntaxError')} in {e.get('file', '?')} "
                    f"at line {e.get('line', '?')}: {e.get('raw_message', e.get('message', ''))}"
                    for e in syntax_errors
                )
                + "\n\nFix the syntax/indentation error(s) so the file is valid Python. "
                "Preserve all logic that is not broken. "
                "Do not refactor or rename anything. "
                "Only fix the exact syntax issue reported."
            ),
            confidence         = 0.95,
            suggested_commands = [
                f"python -m py_compile {ftf.path}" for ftf in files_to_fix
            ],
            files_to_modify = files_to_fix,
            source          = "syntax_error_detected",
        )

        # ── Step 1: dry-run — snapshot files and get the LLM's proposed fix ───
        # We do NOT write to disk yet. Show the diff to the user first.
        solution.files_to_modify = self._normalise_files(solution.files_to_modify)
        self._snapshot_files(solution.files_to_modify)

        try:
            llm_response = fix_files(
                incident_id        = solution.incident_id,
                root_cause         = solution.root_cause,
                healing_prompt     = solution.healing_prompt,
                suggested_commands = solution.suggested_commands,
                files_to_modify    = solution.files_to_modify,
            )
        except Exception as exc:
            logger.error("[SelfHealingAgent] LLM Fixer raised during preview: %s", exc, exc_info=True)
            await self.publish(Event(
                type        = EventType.REMEDIATION_FAILED,
                source      = self.name,
                incident_id = incident_id,
                data        = {
                    "incident_id": incident_id,
                    "service"    : service,
                    "issue_type" : "syntax",
                    "reason"     : f"LLM Fixer failed: {exc}",
                },
            ))
            return

        # ── Step 2: show diff and ask the user ───────────────────────────────
        approved = await self._ask_user_approval(solution, llm_response)
        if not approved:
            logger.info(
                "[SelfHealingAgent] User rejected the proposed fix for %s — aborting",
                incident_id,
            )
            await self.publish(Event(
                type        = EventType.REMEDIATION_FAILED,
                source      = self.name,
                incident_id = incident_id,
                data        = {
                    "incident_id": incident_id,
                    "service"    : service,
                    "issue_type" : "syntax",
                    "reason"     : "User rejected the proposed fix",
                },
            ))
            return

        # ── Step 3: approved — run the full pipeline ─────────────────────────
        result = await self.remediate(solution)

        # Publish result back onto the event bus
        if result.status == RemediationStatus.SUCCESS:
            await self.publish(Event(
                type        = EventType.REMEDIATION_COMPLETE,
                source      = self.name,
                incident_id = incident_id,
                data        = {
                    "incident_id"   : incident_id,
                    "service"       : service,
                    "issue_type"    : "syntax",
                    "files_fixed"   : [m.path for m in result.file_modifications if m.applied],
                    "steps"         : result.steps,
                    "confidence"    : result.confidence,
                    "verification"  : (
                        result.verification.status.value
                        if result.verification else "not_run"
                    ),
                },
            ))
            logger.info(
                "[SelfHealingAgent] ✅ Syntax fix applied for %s — files: %s",
                service,
                [m.path for m in result.file_modifications if m.applied],
            )
        else:
            await self.publish(Event(
                type        = EventType.REMEDIATION_FAILED,
                source      = self.name,
                incident_id = incident_id,
                data        = {
                    "incident_id"      : incident_id,
                    "service"          : service,
                    "issue_type"       : "syntax",
                    "validation_errors": result.validation_errors,
                    "confidence"       : result.confidence,
                },
            ))
            logger.error(
                "[SelfHealingAgent] ❌ Syntax fix FAILED for %s — errors: %s",
                service, result.validation_errors,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Main remediation pipeline — called both by _on_syntax_error and directly
    # ──────────────────────────────────────────────────────────────────────────

    async def remediate(self, solution: Solution) -> SelfHealingResult:
        """
        Full remediation pipeline for one Solution.

        Parameters
        ----------
        solution : Solution produced by Knowledge Agent or built internally
                   from a SYNTAX_ERROR_DETECTED event

        Returns
        -------
        SelfHealingResult with per-file before/after content
        """
        logger.info("[SelfHealingAgent] Starting remediation for %s", solution.incident_id)
        self._active.add(solution.incident_id)

        try:
            return await self._run_pipeline(solution)
        finally:
            self._active.discard(solution.incident_id)

    async def _run_pipeline(self, solution: Solution) -> SelfHealingResult:

        # ── step 1: normalise files_to_modify to List[FileToFix] ─────────────
        # Callers (including old test code) may pass raw dicts — normalise them.
        solution.files_to_modify = self._normalise_files(solution.files_to_modify)

        # ── step 2: guard ─────────────────────────────────────────────────────
        guard_errors = self._guard_solution(solution)
        if guard_errors:
            logger.warning("[SelfHealingAgent] Guard failed: %s", guard_errors)
            return SelfHealingResult(
                incident_id      = solution.incident_id,
                status           = RemediationStatus.FAILED,
                validation_errors = guard_errors,
            )

        # ── step 3: snapshot — read files from disk ───────────────────────────
        self._snapshot_files(solution.files_to_modify)

        # ── step 4: LLM fixer ─────────────────────────────────────────────────
        logger.info("[SelfHealingAgent] Calling LLM Fixer for %s...", solution.incident_id)
        try:
            llm_response: LLMFixResponse = fix_files(
                incident_id        = solution.incident_id,
                root_cause         = solution.root_cause,
                healing_prompt     = solution.healing_prompt,
                suggested_commands = solution.suggested_commands,
                files_to_modify    = solution.files_to_modify,
            )
        except Exception as exc:
            logger.error("[SelfHealingAgent] LLM Fixer raised: %s", exc, exc_info=True)
            return SelfHealingResult(
                incident_id       = solution.incident_id,
                status            = RemediationStatus.FAILED,
                validation_errors = [f"LLM Fixer exception: {exc}"],
            )

        # ── step 5: validate LLM response ─────────────────────────────────────
        known_paths       = {f.path for f in solution.files_to_modify}
        validation_errors = self._validate_llm_response(llm_response, known_paths)
        if validation_errors:
            logger.warning("[SelfHealingAgent] Validation errors: %s", validation_errors)
            return SelfHealingResult(
                incident_id       = solution.incident_id,
                status            = RemediationStatus.FAILED,
                validation_errors = validation_errors,
                llm_response      = llm_response,
            )

        # ── step 5b: guard against destructive overwrites ─────────────────────
        # Catches the case where the LLM chose "overwrite" and silently dropped
        # large portions of the original file instead of doing a targeted fix.
        snapshots_by_path = {f.path: f.current_content for f in solution.files_to_modify}
        shrink_errors = self._detect_suspicious_shrink(llm_response.modified_files, snapshots_by_path)
        if shrink_errors:
            logger.warning("[SelfHealingAgent] Suspicious content shrink detected: %s", shrink_errors)
            return SelfHealingResult(
                incident_id       = solution.incident_id,
                status            = RemediationStatus.FAILED,
                validation_errors = shrink_errors,
                llm_response      = llm_response,
            )

        # ── step 6: build FileModificationResult list ─────────────────────────
        snapshots          = {f.path: f.current_content for f in solution.files_to_modify}
        file_modifications = self._build_modifications(llm_response.modified_files, snapshots)

        # ── step 7: apply to disk ─────────────────────────────────────────────
        if self.apply_changes:
            self._apply_to_disk(file_modifications)

        # ── step 8: execute remediation commands ──────────────────────────────
        command_results: List[CommandExecutionResult] = []
        if self.apply_changes and llm_response.remediation_commands:
            logger.info(
                "[SelfHealingAgent] Executing %d remediation command(s)...",
                len(llm_response.remediation_commands),
            )
            command_results = self._execute_remediation_commands(
                llm_response.remediation_commands
            )

        # ── step 9: verify ────────────────────────────────────────────────────
        verification = None
        if self.apply_changes:
            verification = verify_fix(
                incident_id    = solution.incident_id,
                root_cause     = solution.root_cause,
                healing_prompt = solution.healing_prompt,
                modifications  = file_modifications,
            )
            logger.info("[SelfHealingAgent] Verification: %s", verification)

        files_ok     = all(m.applied or not self.apply_changes for m in file_modifications)
        cmds_ok      = all(c.succeeded or c.on_failure != "abort" for c in command_results)
        final_status = RemediationStatus.SUCCESS if (files_ok and cmds_ok) else RemediationStatus.FAILED
        if final_status == RemediationStatus.FAILED:
            for m in file_modifications:
                if m.error:
                    print(f"[SelfHealingAgent] ✘ {os.path.basename(m.path)}: {m.error}")
            for c in command_results:
                if not c.succeeded and not c.skipped:
                    print(f"[SelfHealingAgent] ✘ command failed: {c.command[:90]}"
                          f"{' — ' + (c.stderr or c.error)[:150] if (c.stderr or c.error) else ''}")

        return SelfHealingResult(
            incident_id                 = solution.incident_id,
            status                      = final_status,
            file_modifications          = file_modifications,
            steps                       = llm_response.steps,
            confidence                  = llm_response.confidence,
            remediation_command_results = command_results,
            llm_response                = llm_response,
            verification                = verification,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Pipeline steps
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_files(files_to_modify) -> List[FileToFix]:
        """
        Accept either List[FileToFix] or List[dict] (old test/caller format).
        Always returns List[FileToFix].
        """
        result = []
        for entry in files_to_modify:
            if isinstance(entry, FileToFix):
                result.append(entry)
            elif isinstance(entry, dict):
                # Support both "path" and "file" keys — monitoring agent uses "file"
                result.append(FileToFix(
                    path            = entry.get("path") or entry.get("file", ""),
                    line            = int(entry.get("line", 0) or 0),
                    function        = entry.get("function", ""),
                    exception       = entry.get("exception", ""),
                    fix_description = entry.get("fix_description", entry.get("content", "")),
                    current_content = entry.get("current_content", entry.get("content", "")),
                ))
            else:
                logger.warning("[SelfHealingAgent] Unknown files_to_modify entry type: %s", type(entry))
        return result

    def _guard_solution(self, solution: Solution) -> List[str]:
        errors = []
        if not solution.files_to_modify:
            # ── fallback: try to extract file+line from the incident description ──
            # This handles the case where log_parser found 0 files but the CI/CD
            # output contains a plain "*** Sorry: IndentationError: ... (main.py, line 11)"
            # that was embedded in the description by groq_analyzer / incident_factory.
            recovered = self._extract_files_from_description(solution)
            if not recovered:
                # e.g. "fastapi==9.9.9 in requirements.txt" — no traceback, but the
                # diagnosis names a real project file.
                recovered = self._files_mentioned_in_solution(solution)
            if recovered:
                logger.info(
                    "[SelfHealingAgent] Guard: recovered %d file(s) from incident description",
                    len(recovered),
                )
                solution.files_to_modify = recovered
            else:
                errors.append("Solution.files_to_modify is empty — nothing to fix.")
                return errors
        for i, f in enumerate(solution.files_to_modify):
            if not f.path.strip():
                errors.append(f"files_to_modify[{i}] missing 'path'.")
        return errors

    # ── patterns reused from log_parser for description scanning ──────────────
    _DESC_SORRY_RE = re.compile(
        r'\*+\s*Sorry:\s*(?P<exc_type>\w+(?:Error|Exception))\s*:\s*'
        r'(?P<message>[^(]+)\((?P<file>[^,\)]+),\s*line\s*(?P<line>\d+)\)',
        re.IGNORECASE,
    )
    _DESC_BARE_RE = re.compile(
        r'(?P<exc_type>(?:Syntax|Indentation|Tab)Error)\s*:\s*'
        r'(?P<message>[^(]+)\((?P<file>[^,\)]+),\s*line\s*(?P<line>\d+)\)',
        re.IGNORECASE,
    )

    def _extract_files_from_description(self, solution: Solution) -> List[FileToFix]:
        """
        Scan the solution's root_cause and healing_prompt for GitHub-Actions-style
        syntax error messages and build FileToFix entries from them.

        Matches:
          *** Sorry: IndentationError: expected an indented block (main.py, line 11)
          SyntaxError: invalid syntax (app.py, line 5)
        """
        search_text = " ".join(filter(None, [
            getattr(solution, "root_cause",     ""),
            getattr(solution, "healing_prompt", ""),
            getattr(solution, "description",    ""),
        ]))

        seen: set = set()
        files: List[FileToFix] = []

        for pattern in (self._DESC_SORRY_RE, self._DESC_BARE_RE):
            for m in pattern.finditer(search_text):
                raw_file = m.group("file").strip()
                lineno   = int(m.group("line"))
                exc_type = m.group("exc_type").strip()
                msg      = m.group("message").strip().rstrip("—- ")

                key = (raw_file, lineno)
                if key in seen:
                    continue
                seen.add(key)

                file_path = str(self._resolve_path(raw_file))
                full_msg  = f"{exc_type}: {msg} ({raw_file}, line {lineno})"

                logger.info(
                    "[SelfHealingAgent] Recovered from description: %s line %d — %s",
                    raw_file, lineno, exc_type,
                )
                files.append(FileToFix(
                    path            = file_path,
                    line            = lineno,
                    function        = "<module>",
                    exception       = full_msg,
                    fix_description = (
                        f"Fix {exc_type} at line {lineno}. "
                        f"Original error: {full_msg}"
                    ),
                ))

        return files

    _SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                  ".devops", ".self_healing_backups", "dist", "build"}

    def _files_mentioned_in_solution(self, solution: Solution) -> List[FileToFix]:
        """Project files whose name/relative path appears in the diagnosis text
        (root cause, healing steps, commands). Only files that exist are returned."""
        import re
        text = " ".join(filter(None, [
            getattr(solution, "root_cause", ""),
            getattr(solution, "healing_prompt", ""),
            " ".join(getattr(solution, "suggested_commands", []) or []),
        ]))
        if not text:
            return []
        root = self._project_root
        found: List[FileToFix] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self._SKIP_DIRS]
            if len(Path(dirpath).relative_to(root).parts) > 4:
                continue
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                # match the relative path or the bare file name as a whole token
                whole_name = r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])"
                if rel in text or re.search(whole_name, text):
                    found.append(FileToFix(
                        path=str(full), line=0, function="<file>",
                        exception=(getattr(solution, "root_cause", "") or "")[:300],
                        fix_description=(getattr(solution, "healing_prompt", "") or "")[:1500],
                    ))
                if len(found) >= 5:
                    break
        if found:
            logger.info("[SelfHealingAgent] Guard: files named in diagnosis → %s",
                        [f.path for f in found])
            print(f"[SelfHealingAgent] Files identified from diagnosis: "
                  f"{', '.join(Path(f.path).name for f in found)}")
        return found

    def _snapshot_files(self, files_to_modify: List[FileToFix]) -> None:
        """Read current content from disk into each FileToFix.current_content."""
        for f in files_to_modify:
            if os.path.isfile(f.path):
                try:
                    with open(f.path, "r", encoding="utf-8") as fh:
                        f.current_content = fh.read()
                    logger.debug("[SelfHealingAgent] Snapshot OK: %s", f.path)
                except OSError as exc:
                    logger.warning("[SelfHealingAgent] Cannot read %s: %s", f.path, exc)
                    f.current_content = ""
            else:
                logger.debug("[SelfHealingAgent] File not on disk (new?): %s", f.path)
                f.current_content = ""

    def _validate_llm_response(
        self,
        llm_response : LLMFixResponse,
        known_paths  : set,
    ) -> List[str]:
        errors = []
        if not llm_response.modified_files:
            errors.append("LLM returned zero modified_files entries.")
            return errors

        for i, entry in enumerate(llm_response.modified_files):
            tag  = f"modified_files[{i}]"
            path = entry.get("path", "").strip()
            if not path:
                errors.append(f"{tag} missing 'path'.")
                continue
            if not entry.get("new_content", "").strip():
                errors.append(f"{tag} (path={path}) missing 'new_content'.")

            # FIX: normalise path separators + resolve before comparing,
            # because the LLM sometimes returns an absolute path when given a relative one.
            resolved = str(Path(path).resolve())
            known_resolved = {str(Path(p).resolve()) for p in known_paths}
            if path not in known_paths and resolved not in known_resolved:
                errors.append(
                    f"{tag} path='{path}' was not in the original files_to_modify. "
                    f"Known: {sorted(known_paths)}"
                )

        return errors

    # Flag ANY non-append action whose new_content is dramatically shorter than
    # the original file. "replace_line" is not exempt: it's written to disk via
    # the exact same full-file "w" write as "overwrite" (see _apply_to_disk), so
    # an LLM that returns partial content under "replace_line" will silently
    # truncate the file just as badly as a bad "overwrite" would. 0.6 = new
    # content must be at least 60% of the original file's length, or it's
    # treated as suspicious. MIN_OLD_CONTENT_LEN avoids false positives on tiny
    # files where any real fix naturally changes a large percentage of the
    # (already-small) file.
    SHRINK_RATIO_THRESHOLD = 0.6
    MIN_OLD_CONTENT_LEN    = 50

    def _detect_suspicious_shrink(
        self,
        modified_files : List[Dict],
        snapshots      : Dict[str, str],
    ) -> List[str]:
        """
        Flag actions where new_content is dramatically shorter than the
        original file — a strong signal the LLM regenerated the file from
        scratch (dropping unrelated functions/routes) instead of preserving it
        and applying a targeted fix, as instructed. Applies to "overwrite" and
        "replace_line" alike, since both are written as a full-file replace on
        disk. "append" is exempt because it only adds content and never
        truncates the file.
        """
        errors = []
        for entry in modified_files:
            action = entry.get("action", "overwrite")
            if action == "append":
                continue  # append only adds content, can't shrink the file

            path = entry.get("path", "").strip()
            old_content = snapshots.get(path) or snapshots.get(str(Path(path).resolve()), "")
            new_content = entry.get("new_content", "")

            old_len = len(old_content.strip())
            new_len = len(new_content.strip())

            if old_len < self.MIN_OLD_CONTENT_LEN:
                continue  # too small a file for the ratio check to be meaningful

            ratio = new_len / old_len if old_len else 1.0
            if ratio < self.SHRINK_RATIO_THRESHOLD:
                errors.append(
                    f"{path}: '{action}' shrank content from {old_len} to {new_len} chars "
                    f"({ratio:.0%} of original) — new_content looks incomplete rather than "
                    f"the full file with a targeted fix applied. Rejected."
                )

        return errors

    @staticmethod
    def _build_modifications(
        modified_files : List[Dict],
        snapshots      : Dict[str, str],
    ) -> List[FileModificationResult]:
        results = []
        for entry in modified_files:
            path = entry["path"].strip()
            # Try both the raw path and its resolved form when looking up snapshot
            old = snapshots.get(path) or snapshots.get(str(Path(path).resolve()), "")
            results.append(FileModificationResult(
                path        = path,
                old_content = old,
                new_content = SelfHealingAgent._strip_code_fences(entry.get("new_content", "")),
                action      = entry.get("action", "overwrite"),
            ))
        return results

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """LLMs often wrap file content in ```lang ... ``` — never write those to disk."""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            lines = lines[1:]                                   # drop ```python
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]                              # drop closing ```
            text = "\n".join(lines) + "\n"
        return text

    @staticmethod
    def _print_diff(mod: FileModificationResult, max_lines: int = 30) -> None:
        """Show exactly what changed, so every fix is visible in the terminal."""
        import difflib
        new = (mod.old_content + mod.new_content) if mod.action == "append" else mod.new_content
        diff = [l for l in difflib.unified_diff(
                    mod.old_content.splitlines(), new.splitlines(), lineterm="", n=0)
                if not l.startswith(("---", "+++", "@@"))]
        print(f"\n  ✎ {os.path.basename(mod.path)} — {len(diff)} line(s) changed")
        for line in diff[:max_lines]:
            color = "\033[31m" if line.startswith("-") else "\033[32m"
            print(f"    {color}{line}\033[0m")
        if len(diff) > max_lines:
            print(f"    ... {len(diff) - max_lines} more")

    def _apply_to_disk(self, modifications: List[FileModificationResult]) -> None:
        timestamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        # Backup root lives inside the user's project, never next to the SDK files
        backup_dir = self._project_root / ".self_healing_backups" / timestamp

        for mod in modifications:
            try:
                if mod.old_content:
                    # Preserve the relative path structure inside the backup folder
                    # so that two files with the same basename don't collide.
                    # e.g. /user_project/src/main.py
                    #   → <project_root>/.self_healing_backups/<ts>/src/main.py.bak
                    try:
                        rel = Path(mod.path).resolve().relative_to(self._project_root)
                    except ValueError:
                        # path is outside project_root — fall back to basename only
                        rel = Path(os.path.basename(mod.path))

                    bak_path = backup_dir / rel.parent / (rel.name + ".bak")
                    bak_path.parent.mkdir(parents=True, exist_ok=True)
                    bak_path.write_text(mod.old_content, encoding="utf-8")
                    mod.backup_path = str(bak_path)
                    logger.info("[SelfHealingAgent] Backup: %s", bak_path)

                if mod.action != "append" and mod.new_content == mod.old_content:
                    mod.applied = True          # nothing to change
                    continue

                # Safety net: never write a Python file that doesn't compile.
                if mod.path.endswith(".py"):
                    candidate = (mod.old_content + mod.new_content) if mod.action == "append" else mod.new_content
                    try:
                        compile(candidate, mod.path, "exec")
                    except SyntaxError as exc:
                        mod.error = f"rejected: generated fix is not valid Python ({exc.msg}, line {exc.lineno})"
                        logger.error("[SelfHealingAgent] %s — %s kept unchanged", mod.error, mod.path)
                        print(f"[SelfHealingAgent] ✘ Fix for {os.path.basename(mod.path)} rejected: "
                              f"{exc.msg} (line {exc.lineno}). Original file kept.")
                        continue

                os.makedirs(os.path.dirname(mod.path) or ".", exist_ok=True)
                write_mode = "a" if mod.action == "append" else "w"
                with open(mod.path, write_mode, encoding="utf-8") as fh:
                    fh.write(mod.new_content)
                mod.applied = True
                self._print_diff(mod)
                logger.info("[SelfHealingAgent] Written (%s): %s", mod.action, mod.path)

            except OSError as exc:
                mod.error = str(exc)
                logger.error("[SelfHealingAgent] Write failed for %s: %s", mod.path, exc)

    # Only harmless, local dependency/check commands may run automatically.
    # Everything else (git checkout/commit/push, docker build, kubectl, cd, file
    # rewrites via sed/PowerShell, deletes) is either done by the pipeline itself
    # or already covered by the file edits — running it here caused branch
    # switches and false "REMEDIATION FAILED" results.
    # pip/npm install are NOT allowed: they would install the user's project
    # dependencies into the agent's own environment (and can break it).
    # The CI pipeline installs dependencies in a clean container anyway.
    _SAFE_PREFIXES = (
        "python -m py_compile", "python -m compileall", "py -m py_compile", "py -m compileall",
    )

    @classmethod
    def _is_safe_command(cls, command: str) -> bool:
        c = " ".join(command.strip().lower().split())
        if any(ch in c for ch in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
            return False
        return c.startswith(cls._SAFE_PREFIXES)

    def _execute_remediation_commands(
        self,
        commands : List[Dict],
        timeout  : int = 120,
    ) -> List[CommandExecutionResult]:
        results: List[CommandExecutionResult] = []
        abort_triggered = False

        for cmd_dict in commands:
            command     = cmd_dict.get("command", "").strip()
            description = cmd_dict.get("description", "")
            order       = int(cmd_dict.get("order", len(results) + 1))
            on_failure  = cmd_dict.get("on_failure", "abort").lower()

            result = CommandExecutionResult(
                command     = command,
                description = description,
                order       = order,
                on_failure  = on_failure,
            )
            results.append(result)

            if command and not self._is_safe_command(command):
                result.skipped    = True
                result.returncode = 0
                result.error      = "skipped — git/docker/deploy steps are done by the pipeline"
                print(f"[SelfHealingAgent] ⏭ skipped (pipeline handles this): {command[:90]}")
                continue

            if abort_triggered:
                result.skipped = True
                logger.warning("[SelfHealingAgent] Skipping (abort active): %r", command)
                continue

            if not command:
                result.error   = "empty command — skipped"
                result.skipped = True
                continue

            attempts = 2 if on_failure == "retry" else 1
            for attempt in range(1, attempts + 1):
                logger.info(
                    "[SelfHealingAgent] Running (order=%d, attempt=%d/%d): %r",
                    order, attempt, attempts, command,
                )
                try:
                    proc = subprocess.run(
                        command, shell=True, capture_output=True,
                        text=True, timeout=timeout, cwd=str(self._project_root),
                    )
                    result.returncode = proc.returncode
                    result.stdout     = proc.stdout.strip()
                    result.stderr     = proc.stderr.strip()

                    if result.succeeded:
                        logger.info("[SelfHealingAgent] ✓ Command OK (order=%d): %r", order, command)
                        break
                    logger.warning(
                        "[SelfHealingAgent] ✗ Command failed (order=%d, rc=%d): %r%s",
                        order, result.returncode, command,
                        f"\n  stderr: {result.stderr}" if result.stderr else "",
                    )
                except subprocess.TimeoutExpired:
                    result.error      = f"timed out after {timeout}s"
                    result.returncode = -1
                    logger.error("[SelfHealingAgent] Timeout (order=%d): %r", order, command)
                    break
                except Exception as exc:
                    result.error      = str(exc)
                    result.returncode = -1
                    logger.error("[SelfHealingAgent] Exception (order=%d): %s", order, exc, exc_info=True)
                    break

            if not result.succeeded and on_failure == "abort":
                logger.error("[SelfHealingAgent] on_failure=abort — halting after order=%d", order)
                abort_triggered = True

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # User approval gate
    # ──────────────────────────────────────────────────────────────────────────

    async def _ask_user_approval(
        self,
        solution     : Solution,
        llm_response : LLMFixResponse,
    ) -> bool:
        """
        Print the proposed file changes as a unified diff and ask the user
        to accept or reject via stdin. Returns True if accepted.

        Uses asyncio.to_thread for the blocking input() call so the event
        loop is not stalled while waiting for the user.
        """
        import difflib

        print("\n" + "═" * 68)
        print("  🔧  SELF-HEALING AGENT — PROPOSED FIX")
        print("═" * 68)
        print(f"  Incident : {solution.incident_id}")
        print(f"  Cause    : {solution.root_cause}")
        print(f"  Files    : {len(llm_response.modified_files)}")
        print("═" * 68)

        snapshots = {f.path: f.current_content for f in solution.files_to_modify}

        for entry in llm_response.modified_files:
            path        = entry.get("path", "?")
            new_content = entry.get("new_content", "")
            old_content = snapshots.get(path, "")

            diff = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            ))

            print(f"\n  File: {path}")
            print("  " + "-" * 60)
            if diff:
                for dl in diff[:60]:   # cap at 60 diff lines for readability
                    prefix = dl[0] if dl else " "
                    color  = "\033[32m" if prefix == "+" else "\033[31m" if prefix == "-" else ""
                    reset  = "\033[0m" if color else ""
                    print(f"  {color}{dl.rstrip()}{reset}")
                if len(diff) > 60:
                    print(f"  ... ({len(diff) - 60} more lines not shown)")
            else:
                print("  (no diff — content unchanged)")

        print("\n" + "═" * 68)
        if llm_response.steps:
            print("  Steps the agent will take:")
            for i, step in enumerate(llm_response.steps, 1):
                print(f"    {i}. {step}")

        if llm_response.remediation_commands:
            print("\n  Commands that will run after the fix:")
            for cmd in llm_response.remediation_commands:
                print(f"    $ {cmd.get('command', '')}")

        print("═" * 68)

        def _prompt() -> bool:
            while True:
                try:
                    ans = input("\n  Accept this fix? [y/N] > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n  [Interrupted — rejecting fix]")
                    return False
                if ans in ("y", "yes"):
                    return True
                if ans in ("n", "no", ""):
                    return False
                print("  Please enter y or n.")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _prompt)

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _resolve_path(self, file_path: str) -> Path:
        """
        Resolve a file path from the event against the project root.
        If it's already absolute, return as-is.
        If relative, join with project_root.
        """
        p = Path(file_path)
        if p.is_absolute():
            return p
        return self._project_root / p