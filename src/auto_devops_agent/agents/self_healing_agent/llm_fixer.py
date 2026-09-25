"""
self_healing/llm_fixer.py
-----------------------------
Called by the Self-Healing Agent after a Solution is produced.
Receives root_cause, healing_prompt, suggested_commands, and files_to_modify.

Step 1: Build a structured prompt for the LLM (senior DevOps persona)
Step 2: Call the LLM to generate new file contents + remediation steps + remediation commands
Step 3: Parse the response and return LLMFixResponse
"""
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

# allow running both as part of the package and standalone
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from agents.self_healing_agent.models import LLMFixResponse, FileToFix
except ImportError:
    from models import LLMFixResponse, FileToFix  # standalone / test mode

from groq import Groq
from groq import RateLimitError, APIStatusError
from dotenv import load_dotenv
import time

load_dotenv()
class _LazyGroq:
    """Create the Groq client on first use, so importing this module
    never crashes when GROQ_API_KEY isn't set yet (e.g. `devops --doctor`)."""
    _client = None

    def __getattr__(self, name):
        if _LazyGroq._client is None:
            _LazyGroq._client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        return getattr(_LazyGroq._client, name)


client = _LazyGroq()

# ── constant ──────────────────────────────────────────────────────────────────
MODEL = "qwen/qwen3.6-27b"  # Groq model — fast and capable for code fixes

# Groq's free/on-demand tier enforces a strict tokens-per-minute cap that counts
# PROMPT + max_tokens together, not just what's actually generated. Keep a safety
# margin under the account's real limit (confirmed 8000 TPM for this model/tier).
TPM_LIMIT       = 8000
TPM_SAFETY_MARGIN = 300   # leave headroom for tokenizer estimation error


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — good enough for budgeting max_tokens."""
    return max(1, len(text) // 4)


def _safe_max_tokens(prompt: str, desired: int = 6000, floor: int = 1500) -> int:
    """
    Compute a max_tokens value that keeps (prompt + max_tokens) under the
    account's TPM limit, so we don't get a 413 rate_limit_exceeded before
    the call even runs. Shrinks toward `floor` as the prompt grows.
    """
    prompt_tokens = _estimate_tokens(prompt)
    budget        = TPM_LIMIT - prompt_tokens - TPM_SAFETY_MARGIN
    return max(floor, min(desired, budget))


MAX_FILE_CHARS_IN_PROMPT = 6000  # ~1500 tokens/file cap — keeps prompt within TPM budget


def _build_files_block(files_to_modify: List[FileToFix]) -> str:
    """
    Render each FileToFix entry with its full enrichment for the prompt.

    Shows the LLM:
      - exact path to modify
      - line number, function, and exception from the traceback
      - one-line fix hint from the monitoring agent
      - current on-disk content so the LLM can decide the correct action
    """
    if not files_to_modify:
        return "No files provided."

    blocks = []
    for i, f in enumerate(files_to_modify, 1):
        current = f.current_content.strip()
        content_display = current if current else "(file does not exist yet — create it)"

        if len(content_display) > MAX_FILE_CHARS_IN_PROMPT:
            omitted = len(content_display) - MAX_FILE_CHARS_IN_PROMPT
            content_display = (
                content_display[:MAX_FILE_CHARS_IN_PROMPT]
                + f"\n... [TRUNCATED — {omitted} more chars omitted to stay under the "
                  f"account's token-per-minute limit; consider a smaller diff or a paid tier]"
            )
            print(f"[LLMFixer] WARNING — {f.path} content truncated by {omitted} chars for prompt budget")

        # Build the enrichment lines — only include fields that have a value
        enrichment_lines = []
        if f.line:
            enrichment_lines.append(f"  fix at         : line {f.line}" + (f"  in {f.function}()" if f.function else ""))
        if f.exception:
            enrichment_lines.append(f"  exception      : {f.exception}")
        if f.fix_description:
            enrichment_lines.append(f"  hint           : {f.fix_description}")

        enrichment_block = "\n".join(enrichment_lines)

        blocks.append(
            f"FILE {i}:\n"
            f"  path           : {f.path}\n"
            + (enrichment_block + "\n" if enrichment_block else "")
            + f"  current content:\n{content_display}"
        )
    return "\n\n".join(blocks)


def _parse_json_block(text: str, key: str) -> any:
    """
    Extract a fenced JSON block that follows a section header.
    e.g.  MODIFIED_FILES:\n```json\n[...]\n```
    """
    marker = f"{key}:"
    if marker not in text:
        return None

    segment = text.split(marker, 1)[1]
    start = segment.find("```json")
    if start == -1:
        return None

    inner = segment[start + 7:]
    end = inner.find("```")
    if end == -1:
        return None

    try:
        return json.loads(inner[:end].strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_steps(text: str) -> List[str]:
    """Extract numbered steps from the STEPS section."""
    steps = []
    in_section = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("STEPS:"):
            in_section = True
            continue
        if in_section:
            if any(stripped.startswith(h) for h in ("MODIFIED_FILES:", "CONFIDENCE:", "REMEDIATION_COMMANDS:")):
                break
            if stripped and stripped[0].isdigit():
                step = stripped.lstrip("0123456789").lstrip(".) ").strip()
                if step:
                    steps.append(step)

    return steps


def _parse_confidence(text: str) -> float:
    """
    Extract the confidence float from the CONFIDENCE section.
    Handles both same-line and next-line formats:
        CONFIDENCE: 0.85
        CONFIDENCE:
        0.85
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("CONFIDENCE:"):
            inline = stripped.split(":", 1)[1].strip()
            if inline:
                try:
                    return max(0.0, min(1.0, float(inline)))
                except ValueError:
                    pass
            for j in range(i + 1, len(lines)):
                next_val = lines[j].strip()
                if next_val:
                    try:
                        return max(0.0, min(1.0, float(next_val)))
                    except ValueError:
                        break
    return 0.70


def _parse_remediation_commands(text: str) -> List[Dict]:
    """
    Extract the REMEDIATION_COMMANDS JSON array from the LLM response.

    Expected format in the response:

        REMEDIATION_COMMANDS:
        ```json
        [
          {
            "command": "systemctl restart nginx",
            "description": "Reload nginx after config update",
            "order": 1,
            "on_failure": "abort"   // "abort" | "continue" | "retry"
          }
        ]
        ```

    Returns a list of command dicts, sorted by "order" if present.
    Falls back to an empty list if the section is missing or unparseable.
    """
    commands = _parse_json_block(text, "REMEDIATION_COMMANDS")
    if not isinstance(commands, list):
        return []

    normalised = []
    for i, cmd in enumerate(commands):
        if not isinstance(cmd, dict) or not cmd.get("command"):
            continue
        normalised.append({
            "command":     cmd.get("command", "").strip(),
            "description": cmd.get("description", ""),
            "order":       int(cmd.get("order", i + 1)),
            "on_failure":  cmd.get("on_failure", "abort"),
        })

    normalised.sort(key=lambda c: c["order"])
    return normalised


# ── main entry ────────────────────────────────────────────────────────────────

def fix_files(
    incident_id        : str,
    root_cause         : str,
    healing_prompt     : str,
    suggested_commands : List[str],
    files_to_modify    : List[FileToFix],
) -> LLMFixResponse:
    """
    Core fixer function.

    Parameters
    ----------
    incident_id        : identifier forwarded from Solution
    root_cause         : one-line diagnosis from the Knowledge Agent
    healing_prompt     : full narrative produced by the Knowledge Agent
    suggested_commands : shell/kubectl/docker commands already identified
    files_to_modify    : List[FileToFix] — each carries path, line, function,
                         exception, fix_description, and current_content
                         (current_content is populated from disk by
                         SelfHealingAgent._snapshot_files() before this call)

    Returns
    -------
    LLMFixResponse with modified_files, steps, remediation_commands, and confidence
    """

    # ── step 1: render file block ─────────────────────────────────────────
    files_block    = _build_files_block(files_to_modify)
    commands_block = "\n".join(suggested_commands) if suggested_commands else "None"

    import platform
    os_name = "Windows (cmd.exe / PowerShell)" if platform.system() == "Windows" else f"Linux ({platform.system()})"

    # ── step 2: build prompt ──────────────────────────────────────────────
    prompt = f"""You are a senior DevOps engineer operating as an autonomous self-healing agent.
You have already diagnosed an incident. Your job now is to:
  1. Examine the CURRENT content of each file listed below (read directly from disk).
  2. Use the exact line number, function name, and exception type provided per file
     to locate the precise change needed — do not guess.
  3. Decide the correct ACTION for each file: "overwrite" (replace entire file),
     "append" (add to end of file), or "replace_line" (targeted line swap).
  4. Produce the EXACT new content for every file, AND the shell commands
     that must be executed AFTER the files are written to fully remediate the incident.

━━━━━━━━━━━━━━━━━━  ENVIRONMENT  ━━━━━━━━━━━━━━━━━━
OPERATING SYSTEM : {os_name}
All commands MUST be valid on this OS. Do not use commands from other platforms.

━━━━━━━━━━━━━━━━━━  INCIDENT CONTEXT  ━━━━━━━━━━━━━━━━━━
INCIDENT ID   : {incident_id}

ROOT CAUSE:
{root_cause}

HEALING PLAN:
{healing_prompt}

SUGGESTED COMMANDS (hints — refine or extend as needed):
{commands_block}

━━━━━━━━━━━━━━━━━━  FILES TO MODIFY  ━━━━━━━━━━━━━━━━━━
Each file entry shows:
  • path           — the file to modify
  • fix at         — exact line number and function where the exception occurred
  • exception      — the exception type and message raised at that location
  • hint           — one-line description of what needs to change
  • current content — the full on-disk content of the file right now

Use "fix at", "exception", and "hint" together to make a precise, targeted fix.

{files_block}

━━━━━━━━━━━━━━━━━━  YOUR TASK  ━━━━━━━━━━━━━━━━━━
For EVERY file listed above:
  • Use the line number and exception to find exactly what is broken.
  • Choose the most appropriate action:
      - "replace_line"  — swap specific lines (STRONGLY PREFERRED. Use this for
                           syntax errors, indentation errors, and any fix confined
                           to one or a few lines — which is true for nearly every
                           incident this agent handles.)
      - "append"        — add lines to the end (use when only adding new content)
      - "overwrite"     — replace the entire file (LAST RESORT — only when the file
                           genuinely needs restructuring, not for a simple syntax fix.)

  ⚠️ CRITICAL — applies to BOTH "replace_line" AND "overwrite" (there is no
  partial-file mode; both are written to disk as the ENTIRE file):
      new_content MUST be the COMPLETE file, character-for-character identical
      to the "current content" shown above, except for the specific lines being
      fixed. Do NOT drop, rename, shorten, or "clean up" any function, route,
      import, or block you were not asked to change. Returning only the
      changed function/lines under "replace_line" will DELETE every other line
      in the file — this is a critical failure, not an improvement.
  • Produce the COMPLETE new file content — no placeholders, no ellipsis.
  • Preserve all lines that do not need to change.
  • While editing a file, ALSO fix any other definite bug in that same file that would
    crash it at import/startup (e.g. `app = FastAPI` instead of `app = FastAPI()`,
    a missing import). Mention each extra fix in STEPS. Do not refactor or restyle.
  • new_content is raw file text: NO markdown code fences (```), no commentary.

For REMEDIATION_COMMANDS:
  • Use the SUGGESTED COMMANDS above as your primary source — they are already correct.
  • You may add extra commands (e.g. health checks) but keep them minimal.
  • NEVER include git, docker, kubectl, cd, sed or file-editing commands: the pipeline
    commits, pushes and rebuilds by itself, and file changes go in MODIFIED_FILES.
    Also no pip/npm install. Only `python -m py_compile <file>` checks are allowed.
  • Order them by execution sequence using the "order" field (1 = first).
  • Set "on_failure" to one of: "abort" (stop), "continue" (skip and proceed), "retry" (retry once).

  STRICT RULES:
  ① NEVER use `pip install -r <file>` — always install packages directly: `pip install pkg==ver`
  ② NEVER use `systemctl`, `service`, `initctl` — not available on all platforms
  ③ NEVER use Linux-only commands (grep, curl, cat, ls) on Windows — use Python or pip instead
  ④ Health checks must use `on_failure="continue"` — the service may not be running
  ⑤ Every command must be valid on: {os_name}

Respond in this EXACT format (do not add extra sections):

STEPS:
1. <what you changed and why — one file or logical group per step>
2. ...

MODIFIED_FILES:
```json
[
  {{
    "path": "<same path as above>",
    "action": "<overwrite | append | replace_line>",
    "new_content": "<full file text with \\n for newlines>"
  }}
]
```

REMEDIATION_COMMANDS:
```json
[
  {{
    "command": "<exact shell/kubectl/docker command to run>",
    "description": "<one-line explanation of what this command does and why>",
    "order": <integer, 1 = first>,
    "on_failure": "<abort | continue | retry>"
  }}
]
```

CONFIDENCE:
<float 0.0–1.0>
"""

    # ── step 3: call model ────────────────────────────────────────────────
    from agents.self_healing_agent import llm_backend
    print(f"[LLMFixer] Calling {llm_backend.model_name(MODEL)} for incident {incident_id}...")

    max_tokens = _safe_max_tokens(prompt, desired=6000, floor=1500)
    print(f"[LLMFixer] prompt≈{_estimate_tokens(prompt)} tokens, "
          f"requesting max_tokens={max_tokens} (TPM cap={TPM_LIMIT})")

    def _call(mt: int):
        return llm_backend.create(
            default_model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=mt,
            reasoning_effort="none",    # deterministic formatting task, not deep reasoning
            reasoning_format="hidden",  # strip <think> blocks out of message.content
            temperature=0.6,            # Groq's recommended setting for qwen3.6-27b on coding
        )

    try:
        response = _call(max_tokens)
    except (RateLimitError, APIStatusError) as exc:
        status = getattr(exc, "status_code", None)
        if status in (413, 429):
            # Back off once and retry with a smaller ask — covers both "request too
            # large" (413) and a transient per-minute exhaustion (429).
            retry_tokens = max(500, max_tokens // 2)
            print(f"[LLMFixer] {status} from Groq ({exc}). "
                  f"Retrying once with max_tokens={retry_tokens} after a short wait...")
            time.sleep(5)
            response = _call(retry_tokens)
        else:
            raise

    raw = response.choices[0].message.content or ""

    if not raw.strip():
        print(f"[LLMFixer] WARNING — empty response body for {incident_id}. "
              f"finish_reason={response.choices[0].finish_reason!r}")

    # ── step 4: parse response ────────────────────────────────────────────
    modified_files       = _parse_json_block(raw, "MODIFIED_FILES") or []
    steps                = _parse_steps(raw)
    remediation_commands = _parse_remediation_commands(raw)
    confidence           = _parse_confidence(raw)

    if not modified_files:
        # Don't let this vanish silently — dump enough of the raw response
        # to diagnose why parsing failed (truncation, wrong section headers, etc.)
        print(f"[LLMFixer] WARNING — 0 modified_files parsed. Raw response "
              f"({len(raw)} chars):\n{raw[:1500]}")

    print(
        f"[LLMFixer] Done — "
        f"files={len(modified_files)}, "
        f"steps={len(steps)}, "
        f"cmds={len(remediation_commands)}, "
        f"confidence={confidence}"
    )
    for i, step in enumerate(steps, 1):
        print(f"    {i}. {str(step).strip()[:200]}")

    return LLMFixResponse(
        incident_id          = incident_id,
        modified_files       = modified_files,
        steps                = steps,
        remediation_commands = remediation_commands,
        confidence           = confidence,
        raw_response         = raw,
    )