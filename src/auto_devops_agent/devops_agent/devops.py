import sys
import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime

# ── paths ──────────────────────────────────────────────────────────────────────
DEVOPS_AGENT_DIR = Path(__file__).resolve().parent
ROOT             = DEVOPS_AGENT_DIR.parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEVOPS_AGENT_DIR))

from controllers.agent_controller import AgentController
from agents.scaffold_agent.shared.config import load_config as load_scaffold_config
from agents.scaffold_agent.core_scaffold.scaffold_agent import ScaffoldAgent
from core.orchestrator import Orchestrator
from core.event_bus import EventType
from core.project_db import ProjectDB
from dotenv import load_dotenv

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ── LLM Provider selector ──────────────────────────────────────────────────────
try:
    from providers.llm.llm_selector import (
        get_llm_provider,
        handle_quota_error,
        is_quota_error,
        get_all_agent_configs,
    )
    _LLM_SELECTOR_AVAILABLE = True
except ImportError:
    _LLM_SELECTOR_AVAILABLE = False

# ── Email client ───────────────────────────────────────────────────────────────
try:
    from core.email_client import EmailClient
    _EMAIL_MODULE_AVAILABLE = True
except ImportError:
    _EMAIL_MODULE_AVAILABLE = False

# ── Optional agents ────────────────────────────────────────────────────────────
try:
    from agents.cicd_agent.cicd_agent import CICDAgent
    from providers.cicd.github_provider import GitHubProvider
    _CICD_AVAILABLE = bool(GITHUB_TOKEN)
except ImportError:
    _CICD_AVAILABLE = False

try:
    from agents.monitoring_agent.agent import MonitoringAgent
    from agents.monitoring_agent.config import MonitoringConfig
    _MONITORING_AVAILABLE = True
except ImportError:
    _MONITORING_AVAILABLE = False


def _load_knowledge_adapter():
    # Import via fully-qualified package path only.
    # Never add knowledge_agent root to sys.path — both scaffold_agent and
    # knowledge_agent have a 'shared/' sub-package; adding the root causes
    # Python to resolve bare 'shared.models' to scaffold_agent/shared/models.py.
    try:
        from agents.knowledge_agent.knowledge_core.knowledge_agent_adapter import KnowledgeAgentAdapter
        return KnowledgeAgentAdapter
    except Exception as exc:
        return exc


_ka_result           = _load_knowledge_adapter()
_KNOWLEDGE_AVAILABLE = not isinstance(_ka_result, Exception)
if _KNOWLEDGE_AVAILABLE:
    KnowledgeAgentAdapter = _ka_result

# ── Silence noisy loggers ──────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(message)s")
for _lib in ["agents", "core", "httpx", "aiohttp", "urllib3", "groq",
             "qdrant_client", "sentence_transformers", "huggingface",
             "transformers", "filelock", "PIL"]:
    logging.getLogger(_lib).setLevel(logging.ERROR)

# ── ANSI ───────────────────────────────────────────────────────────────────────
import ctypes as _ctypes


def _ansi_supported() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        kernel32 = _ctypes.windll.kernel32
        handle   = kernel32.GetStdHandle(-11)
        mode     = _ctypes.c_ulong(0)
        if not kernel32.GetConsoleMode(handle, _ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_ANSI = _ansi_supported()
_R  = "\033[0m"  if _ANSI else ""
_B  = "\033[1m"  if _ANSI else ""
_D  = "\033[2m"  if _ANSI else ""
_CY = "\033[36m" if _ANSI else ""
_GR = "\033[32m" if _ANSI else ""
_YL = "\033[33m" if _ANSI else ""
_RD = "\033[31m" if _ANSI else ""

_LOGO = [
    "",
    f"  {_B}{_CY}██████╗ ███████╗██╗   ██╗ ██████╗ ██████╗ ███████╗{_R}",
    f"  {_B}{_CY}██╔══██╗██╔════╝██║   ██║██╔═══██╗██╔══██╗██╔════╝{_R}",
    f"  {_B}{_CY}██║  ██║█████╗  ██║   ██║██║   ██║██████╔╝███████╗{_R}",
    f"  {_B}{_CY}██║  ██║██╔══╝  ╚██╗ ██╔╝██║   ██║██╔═══╝ ╚════██║{_R}",
    f"  {_B}{_CY}██████╔╝███████╗ ╚████╔╝ ╚██████╔╝██║     ███████║{_R}",
    f"  {_B}{_CY}╚═════╝ ╚══════╝  ╚═══╝   ╚═════╝ ╚═╝     ╚══════╝{_R}",
    f"  {_D}{'─'*52}{_R}",
    f"  {_D}Autonomous DevOps  ·  Scaffold · CI/CD · Monitor · Heal{_R}",
    "",
]


def _print_logo():
    for line in _LOGO:
        print(line)



# ── Email client builder ───────────────────────────────────────────────────────

def _build_email_client():
    """
    Build EmailClient from ALERT_* env vars.

    ALERT_ENGINEER_EMAIL  — primary developer; receives all approvals and alerts.
    ALERT_TEAM_EMAILS     — comma-separated; receives alerts when HIGH / CRITICAL.

    Returns None silently if required vars are missing.
    """
    if not _EMAIL_MODULE_AVAILABLE:
        return None

    host     = os.getenv("ALERT_SMTP_HOST",     "").strip()
    port_s   = os.getenv("ALERT_SMTP_PORT",     "587").strip()
    username = os.getenv("ALERT_SMTP_USERNAME", "").strip()
    password = os.getenv("ALERT_SMTP_PASSWORD", "").strip()
    from_    = os.getenv("ALERT_FROM_ADDRESS",  username).strip()
    engineer = os.getenv("ALERT_ENGINEER_EMAIL","").strip()

    team_raw = os.getenv("ALERT_TEAM_EMAILS", "").strip()
    team     = [a.strip() for a in team_raw.split(",") if a.strip()] if team_raw else []

    if not (host and username and password and engineer):
        return None

    try:
        client = EmailClient(
            smtp_host         = host,
            smtp_port         = int(port_s),
            username          = username,
            password          = password,
            from_address      = from_,
            engineer_email    = engineer,
            team_emails       = team,
            approval_base_url = os.getenv("ALERT_APPROVAL_BASE_URL", "").strip(),
        )
        team_info = f"  |  emergency team → {len(team)} address(es)" if team else ""
        print(f"  [Email] approvals/alerts → {engineer}{team_info}")
        return client
    except Exception as exc:
        print(f"  [Email] failed to init: {exc}")
        return None


# ── Dashboard ──────────────────────────────────────────────────────────────────
#
# FIX: Dashboard interference with agent output.
#
# ROOT CAUSE: The dashboard uses ANSI cursor-up sequences to redraw in-place.
# When an agent prints output while the dashboard is "running", the next
# redraw moves the cursor up N lines and overwrites the agent's output.
#
# SOLUTION:
#   1. pause() sets _first_draw=True so next draw starts FRESH (no cursor-up)
#   2. resume() does NOT immediately redraw — waits for the 5s loop tick
#   3. Loop interval is 5s (was 2s) — less interruption
#   4. Heavy agent output events (SCAFFOLD_STARTED, INCIDENT_CREATED, etc.)
#      automatically call pause() before their output and resume() after
#
# Result: agent output is always visible, dashboard only redraws when safe.

class Dashboard:

    def __init__(self, orchestrator: Orchestrator):
        self._orch        = orchestrator
        self._stage       = "INIT"
        self._project     = ""
        self._repo        = ""
        self._cicd_status = ""
        self._last_event  = ""
        self._start       = datetime.utcnow()
        self._paused      = False
        self._first_draw  = True
        self._last_lines  = 0
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop(), name="dashboard")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
        sys.stdout.write("\n")
        sys.stdout.flush()

    def pause(self):
        """
        Stop dashboard redraws and reset to fresh-draw mode.
        After pause(), the next _draw() will print below current output
        instead of moving cursor up — so agent output is preserved.
        """
        self._paused     = True
        self._first_draw = True  # forces fresh draw on next resume

    def resume(self):
        """Resume redraws. Does NOT draw immediately — waits for loop tick."""
        self._paused = False

    def set_stage(self, stage: str):
        self._stage = stage

    def event(self, msg: str):
        self._last_event = msg

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(5)  # 5s interval — less interference
                if not self._paused:
                    self._draw()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _draw(self):
        if self._paused:
            return

        now    = datetime.utcnow()
        up     = int((now - self._start).total_seconds())
        um, us = divmod(up, 60)
        uh, um = divmod(um, 60)
        W      = 60

        sc = {
            "SCAFFOLD" : _CY, "CICD"    : _CY,
            "MONITORING": _YL, "INCIDENT": _RD,
            "DONE"     : _GR,  "INIT"   : _D,
        }.get(self._stage, _CY)

        reg = self._orch.registry
        sm  = self._orch.state_manager

        def agent_row(name: str) -> str:
            rec    = reg.get(name)
            status = sm.get_agent_status(name) if rec else None
            if not rec or status is None:
                return f"  {_D}○ {name:<24}  —{_R}"
            sv     = status.value
            sc2    = _YL if sv == "running" else _GR if sv == "idle" else _RD
            bullet = "▶" if sv == "running" else "●"
            return f"  {_B}{bullet}{_R} {name:<24}  {_B}{sc2}{sv.upper()}{_R}"

        incidents = sm.get_active_incidents() if hasattr(sm, "get_active_incidents") else []

        def inc_row(inc) -> str:
            ts   = inc.created_at.strftime("%H:%M:%S") if hasattr(inc, "created_at") else ""
            sev  = inc.severity.value if hasattr(inc, "severity") else "?"
            sc3  = _RD if sev in ("critical", "high") else _YL
            st   = inc.status.value if hasattr(inc, "status") else "?"
            stc  = _YL if st != "resolved" else _GR
            desc = (inc.description[:30] + "…") if hasattr(inc, "description") and len(inc.description) > 30 else getattr(inc, "description", "")
            return (
                f"  {_D}{ts}{_R}  {_B}{sc3}{sev.upper():<8}{_R}"
                f"  {_D}{inc.service:<18}{_R}  {_B}{stc}{st}{_R}\n"
                f"           {_D}{desc}{_R}"
            )

        lines = []
        div   = f"  {_D}{'─'*W}{_R}"

        lines.append(div)
        lines.append(
            f"  {_B}Stage   {_R} {_B}{sc}{self._stage:<10}{_R}"
            f"  {_D}uptime {uh:02d}:{um:02d}:{us:02d}{_R}"
        )
        if self._project:
            short = self._project if len(self._project) <= 48 else "…" + self._project[-47:]
            lines.append(f"  {_B}Project {_R} {_D}{short}{_R}")
        if self._repo:
            lines.append(f"  {_B}Repo    {_R} {_D}{self._repo}{_R}")
        if self._cicd_status:
            cc = _GR if "success" in self._cicd_status else _RD if "fail" in self._cicd_status else _YL
            lines.append(f"  {_B}CI/CD   {_R} {_B}{cc}{self._cicd_status.upper()}{_R}")

        lines.append(div)
        lines.append(f"  {_B}AGENTS{_R}")
        for a in ["scaffold_agent", "cicd_agent", "monitoring_agent",
                  "knowledge_agent", "self_healing_agent", "alerting_agent"]:
            lines.append(agent_row(a))

        lines.append(div)
        lines.append(f"  {_B}INCIDENTS{_R}  {_D}({len(incidents)} active){_R}")
        if incidents:
            for inc in incidents:
                lines.append(inc_row(inc))
        else:
            lines.append(f"  {_D}none{_R}")

        lines.append(div)
        lines.append(f"  {_D}{self._last_event}{_R}")
        lines.append(div)

        if self._first_draw or not _ANSI:
            # Fresh draw — just print, no cursor movement
            sys.stdout.write("\n".join(lines) + "\n")
            self._first_draw = False
            self._last_lines = len(lines)
        else:
            # In-place redraw — only safe when nothing else has printed
            n = self._last_lines
            sys.stdout.write(f"\033[{n+1}A\033[J" + "\n".join(lines) + "\n")
            self._last_lines = len(lines)

        sys.stdout.flush()


# ── Approval wrapper ───────────────────────────────────────────────────────────

def _patch_approval(approval_manager, dashboard: Dashboard):
    original = approval_manager.request_approval

    async def patched(title, details=None, context=None):
        dashboard.pause()
        await asyncio.sleep(0.1)
        try:
            return await original(title=title, details=details, context=context)
        finally:
            dashboard.resume()

    approval_manager.request_approval = patched


# ── LLM provider selection ────────────────────────────────────────────────────

def _select_llm_providers_upfront(dashboard: Dashboard) -> dict:
    if not _LLM_SELECTOR_AVAILABLE:
        return {}

    providers = {}
    # Knowledge + Self-Healing models are chosen later, only when an incident
    # actually needs them (see _attach_lazy_llm_choice).
    agents = [
        ("scaffold",  "Scaffold Agent  — generates Dockerfile, k8s, CI/CD"),
    ]

    print(f"\n{'═'*55}")
    print(f"  {_B}LLM Provider Setup{_R}")
    print(f"  Configure the AI model for each agent.")
    print(f"{'─'*55}")

    for agent_key, label in agents:
        try:
            provider = get_llm_provider(agent=agent_key)
            providers[agent_key] = provider
        except Exception as e:
            print(f"  {_YL}Skipped {agent_key}: {e}{_R}")

    print(f"{'═'*55}\n")
    return providers


def _attach_llm_providers_to_orchestrator(orchestrator, dashboard, providers):
    if not _LLM_SELECTOR_AVAILABLE or not providers:
        return

    _AGENT_KEY_MAP = {
        "scaffold_agent"    : "scaffold",
        "knowledge_agent"   : "knowledge",
        "self_healing_agent": "healing",
    }

    def _inject_provider(agent_name, agent_obj):
        selector_key = _AGENT_KEY_MAP.get(agent_name)
        if not selector_key:
            return
        provider = providers.get(selector_key)
        if not provider:
            return
        if hasattr(agent_obj, "set_llm_provider"):
            agent_obj.set_llm_provider(provider)
        if not hasattr(orchestrator, "llm_providers"):
            orchestrator.llm_providers = {}
        orchestrator.llm_providers[agent_name] = provider

    # agents already registered at startup get their provider right away
    for _name in _AGENT_KEY_MAP:
        _a = orchestrator.registry.get_agent(_name)
        if _a:
            _inject_provider(_name, _a)

    _orig_scaffold = orchestrator._on_scaffold_started
    async def _wrapped_scaffold(event):
        agent = orchestrator.registry.get_agent("scaffold_agent")
        if agent:
            _inject_provider("scaffold_agent", agent)
        await _orig_scaffold(event)
    orchestrator._on_scaffold_started = _wrapped_scaffold
    # replace ONLY the original handler (old code cleared a string key that never
    # matched, so every event ran twice: once without the chosen LLM provider)
    _subs = orchestrator.event_bus._subscribers.get(EventType.SCAFFOLD_STARTED, [])
    while _orig_scaffold in _subs:
        _subs.remove(_orig_scaffold)
    orchestrator.event_bus.subscribe(EventType.SCAFFOLD_STARTED, _wrapped_scaffold)

    _orig_incident = orchestrator._on_incident_created
    async def _wrapped_incident(event):
        agent = orchestrator.registry.get_agent("knowledge_agent")
        if agent:
            _inject_provider("knowledge_agent", agent)
        # syntax errors skip the knowledge step and go straight to self-healing
        healer = orchestrator.registry.get_agent("self_healing_agent")
        if healer:
            _inject_provider("self_healing_agent", healer)
        await _orig_incident(event)
    orchestrator._on_incident_created = _wrapped_incident
    # replace ONLY the original handler (old code cleared a string key that never
    # matched, so every event ran twice: once without the chosen LLM provider)
    _subs = orchestrator.event_bus._subscribers.get(EventType.INCIDENT_CREATED, [])
    while _orig_incident in _subs:
        _subs.remove(_orig_incident)
    orchestrator.event_bus.subscribe(EventType.INCIDENT_CREATED, _wrapped_incident)

    _orig_investigation = orchestrator._on_investigation_complete
    async def _wrapped_investigation(event):
        agent = orchestrator.registry.get_agent("self_healing_agent")
        if agent:
            _inject_provider("self_healing_agent", agent)
        await _orig_investigation(event)
    orchestrator._on_investigation_complete = _wrapped_investigation
    # replace ONLY the original handler (old code cleared a string key that never
    # matched, so every event ran twice: once without the chosen LLM provider)
    _subs = orchestrator.event_bus._subscribers.get(EventType.INVESTIGATION_COMPLETE, [])
    while _orig_investigation in _subs:
        _subs.remove(_orig_investigation)
    orchestrator.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE, _wrapped_investigation)


# ── Lazy LLM choice for Knowledge / Self-Healing ─────────────────────────────
# Asked at the moment the agent is about to work on an incident, not at startup.
# Offers the last choice as the default (Enter = keep), so it stays one keypress.

_LAZY_AGENTS = {
    # registry name        selector key   method called by the orchestrator   label
    "knowledge_agent"   : ("knowledge",   "run",        "Knowledge Agent — investigates the incident"),
    "self_healing_agent": ("healing",     "remediate",  "Self-Healing Agent — writes the fix"),
}


def _attach_lazy_llm_choice(orchestrator, dashboard):
    if not _LLM_SELECTOR_AVAILABLE:
        return
    session_choice: dict = {}

    def _choose_now(key: str, label: str):
        last = session_choice.get(key)
        if last is None:
            try:
                last = get_llm_provider(agent=key, use_saved=True)   # last run's choice
            except Exception:
                last = None
        dashboard.pause()
        try:
            if last is not None:
                name = f"{getattr(last, 'name', '?').upper()} / {getattr(last, 'default_model', '?')}"
                ans = input(f"\n  {_B}{label}{_R}\n  Use {name}?  [Enter = yes, c = choose another]: ").strip().lower()
                if ans not in ("c", "change"):
                    session_choice[key] = last
                    return last
            prov = get_llm_provider(agent=key)
            session_choice[key] = prov
            return prov
        except Exception as e:
            print(f"  {_YL}Model selection skipped ({e}) — keeping previous choice{_R}")
            return last
        finally:
            dashboard.resume()

    for agent_name, (key, method, label) in _LAZY_AGENTS.items():
        agent = orchestrator.registry.get_agent(agent_name)
        if agent is None or getattr(agent, "_lazy_llm_wrapped", False):
            continue
        original = getattr(agent, method, None)
        if original is None:
            continue

        def _ensure(agent=agent, key=key, label=label, agent_name=agent_name):
            prov = _choose_now(key, label)
            if prov is not None and hasattr(agent, "set_llm_provider"):
                agent.set_llm_provider(prov)
                if not hasattr(orchestrator, "llm_providers"):
                    orchestrator.llm_providers = {}
                orchestrator.llm_providers[agent_name] = prov

        if asyncio.iscoroutinefunction(original):
            async def _wrapped(*a, _orig=original, _ensure=_ensure, **k):
                _ensure()
                return await _orig(*a, **k)
        else:
            def _wrapped(*a, _orig=original, _ensure=_ensure, **k):
                _ensure()
                return _orig(*a, **k)
        setattr(agent, method, _wrapped)
        agent._lazy_llm_wrapped = True


# ── Event tracker — pauses dashboard around heavy output ──────────────────────

# Events that trigger heavy agent output → pause before, resume after
_PAUSE_BEFORE = {
    EventType.SCAFFOLD_STARTED,
    EventType.INCIDENT_CREATED,
    EventType.INVESTIGATION_COMPLETE,
}
_RESUME_AFTER = {
    EventType.SCAFFOLD_COMPLETE,
    EventType.SCAFFOLD_FAILED,
    EventType.DEPLOYMENT_COMPLETE,
    EventType.REMEDIATION_COMPLETE,
    EventType.REMEDIATION_FAILED,
}


# ── Main ───────────────────────────────────────────────────────────────────────

def _project_fingerprint(project_path: str) -> str:
    """Hash of the project's files (not .git, envs, agent state). Used to detect
    'no progress': a fix that leaves the project in a state we've already seen."""
    import hashlib
    skip = {".git", ".devops", ".self_healing_backups", "__pycache__", "node_modules",
            ".venv", "venv", ".mypy_cache", ".pytest_cache"}
    h = hashlib.sha256()
    root = Path(project_path)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in skip and not (Path(dirpath, d) / "pyvenv.cfg").exists())
        for name in sorted(filenames):
            if name in (".devops_state",):
                continue
            f = Path(dirpath, name)
            try:
                if f.stat().st_size > 5_000_000:
                    continue
                h.update(str(f.relative_to(root)).encode())
                h.update(f.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


# What happened in the last pipeline run (used by main() for auto re-runs)
_LAST_RUN = {"cicd": "", "healed": False, "repo_url": ""}


async def _run_scaffold(rerun: bool = False, auto: bool = False):
    _LAST_RUN["cicd"], _LAST_RUN["healed"] = "", False
    project_path    = str(Path.cwd())
    scaffold_config = load_scaffold_config()

    # ── Build email client from ALERT_* env vars ──────────────────────────
    # Only show channels banner on first run — skip on reruns to reduce noise
    if not rerun:
        print("\n  Checking notification channels...")
        email = _build_email_client()
        if not email:
            print("  [Channels] CLI only — add ALERT_SMTP_* to .env to enable email approvals/alerts")
        print()
    else:
        email = _build_email_client()

    orchestrator = Orchestrator(email=email)
    orchestrator.auto_mode     = auto
    orchestrator.last_repo_url = _LAST_RUN.get("repo_url", "")
    dashboard    = Dashboard(orchestrator)

    # ── ProjectDB: open (or create) the project's history database ────────
    # SQLite by default: <project_path>/.devops/history.db
    # PostgreSQL: set DATABASE_URL env var
    _db = ProjectDB.for_project(project_path)
    orchestrator.set_project_db(_db)

    _patch_approval(orchestrator.approval, dashboard)

    state_file = Path(project_path) / ".devops_state"
    first_run  = not state_file.exists()

    # Only print logo on first run
    if not rerun:
        _print_logo()

    _SCAFFOLD_FILES = [
        "Dockerfile", "docker-compose.yml", ".dockerignore",
        ".github/workflows/deploy.yml",
        "k8s/deployment.yaml", "k8s/service.yaml", "k8s/ingress.yaml",
    ]
    _existing      = [f for f in _SCAFFOLD_FILES if (Path(project_path) / f).exists()]
    _skip_scaffold = False
    _run_flow      = True

    if auto:
        # automatic re-run after an approved fix: keep files, no questions
        _skip_scaffold = bool(_existing)
    elif not first_run:
        if _existing:
            dashboard.pause()
            print(f"\n{'─'*55}")
            print(f"  This project was previously deployed.")
            print(f"  Scaffold files already exist ({len(_existing)} files).")
            print(f"{'─'*55}")
            try:
                answer = input("  Re-generate scaffold files? [yes/no]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "no"
            dashboard.resume()
            if answer not in ("yes", "y"):
                _skip_scaffold = True

        dashboard.pause()
        print(f"\n{'─'*55}")
        print(f"  Run the full DevOps pipeline again?")
        print(f"  (Scaffold → CI/CD → Monitor → Heal)")
        print(f"{'─'*55}")

        if _LLM_SELECTOR_AVAILABLE:
            saved = get_all_agent_configs()
            if saved:
                print(f"  {_D}Saved LLM providers:{_R}")
                for k, v in saved.items():
                    if isinstance(v, dict):
                        print(f"    {_D}{k:<12} → {v.get('provider','?').upper()} / {v.get('model','?')}{_R}")
                print()

        try:
            answer = input("  Proceed? [yes/no]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "no"
        dashboard.resume()

        if answer not in ("yes", "y"):
            _run_flow = False
            print("  Skipping pipeline — launching chat agent.\n")

    # ── Select LLM providers upfront ─────────────────────────────────────────────────
    # On rerun: reuse saved LLM configs silently without asking again
    if rerun and _LLM_SELECTOR_AVAILABLE:
        saved = get_all_agent_configs()
        if saved:
            llm_providers = {}
            for agent_key in ("scaffold",):
                try:
                    llm_providers[agent_key] = get_llm_provider(
                        agent=agent_key, use_saved=True
                    )
                except TypeError:
                    # older version of get_llm_provider — no use_saved param
                    try:
                        llm_providers[agent_key] = get_llm_provider(agent=agent_key)
                    except Exception:
                        pass
                except Exception:
                    pass
            if llm_providers:
                _names = ", ".join(
                    f"{k}: {getattr(v, 'name', '?')} / {getattr(v, 'default_model', getattr(v, 'model', '?'))}"
                    for k, v in llm_providers.items()
                )
                print(f"  {_D}Reusing saved LLM providers — {_names}{_R}\n")
        else:
            llm_providers = _select_llm_providers_upfront(dashboard)
    else:
        llm_providers = _select_llm_providers_upfront(dashboard)
    _attach_llm_providers_to_orchestrator(orchestrator, dashboard, llm_providers)
    _attach_lazy_llm_choice(orchestrator, dashboard)

    # ── Register agents ───────────────────────────────────────────────────
    orchestrator.register_agent("scaffold_agent", ScaffoldAgent(scaffold_config))
    dashboard._project = project_path
    dashboard.event("scaffold ready")

    if _CICD_AVAILABLE:
        provider   = GitHubProvider(token=GITHUB_TOKEN, org="")
        cicd_agent = CICDAgent(
            provider    = provider,
            event_bus   = orchestrator.event_bus,
            registry    = orchestrator.registry,
            state       = orchestrator.state_manager,
            ctx_manager = orchestrator.context_manager,
        )
        await cicd_agent.start()
        orchestrator.register_agent("cicd_agent", cicd_agent)
        dashboard.event("scaffold + cicd ready")
    else:
        dashboard.event("cicd skipped — no GITHUB_TOKEN")

    if _MONITORING_AVAILABLE:
        monitoring_agent = MonitoringAgent(
            event_bus       = orchestrator.event_bus,
            registry        = orchestrator.registry,
            config          = MonitoringConfig(
                collector_backend = "file",
                log_dir           = "logs",
                poll_interval     = 30.0,
            ),
            context_manager = orchestrator.context_manager,
            state_manager   = orchestrator.state_manager,
            groq_api_key    = os.getenv("GROQ_API_KEY"),
            live_dashboard  = False,
        )
        await monitoring_agent.start()
        orchestrator.register_agent("monitoring_agent", monitoring_agent)
        dashboard.event("monitoring started")

    if _KNOWLEDGE_AVAILABLE:
        try:
            orchestrator.register_agent("knowledge_agent", KnowledgeAgentAdapter())
            dashboard.event("knowledge agent registered")
        except Exception as e:
            dashboard.event(f"knowledge skipped — {e}")
    else:
        err = str(_ka_result) if isinstance(_ka_result, Exception) else "unavailable"
        dashboard.event(f"knowledge unavailable — {err}")

    await orchestrator.start()
    await orchestrator.start_approval_server()

    # ── Event tracker ──────────────────────────────────────────────────────
    orig_pub = orchestrator.event_bus.publish

    async def _tracked(event):
        # Pause dashboard BEFORE events that produce heavy output
        if event.type in _PAUSE_BEFORE:
            dashboard.pause()

        _stage_map = {
            EventType.SCAFFOLD_STARTED     : ("SCAFFOLD",   "scaffold agent running"),
            EventType.SCAFFOLD_COMPLETE    : ("CICD",       lambda e: f"scaffold done — {e.data.get('framework','?')} · {len(e.data.get('generated_files',[]))} files"),
            EventType.SCAFFOLD_FAILED      : ("DONE",       lambda e: f"scaffold failed — {e.data.get('error','')}"),
            EventType.DEPLOYMENT_COMPLETE  : ("MONITORING", lambda e: f"ci/cd {e.data.get('status','?')} — {len(e.data.get('logs',[]))} log lines"),
            EventType.INCIDENT_CREATED     : ("INCIDENT",   lambda e: f"incident [{e.data.get('severity','?').upper()}] — {e.data.get('service','?')}"),
            EventType.INVESTIGATION_COMPLETE:("MONITORING", "knowledge agent investigation complete"),
            EventType.REMEDIATION_COMPLETE : ("DONE",       "remediation complete — incident resolved"),
            EventType.REMEDIATION_FAILED   : ("DONE",       "remediation failed — manual intervention needed"),
        }
        if event.type in _stage_map:
            stage, msg = _stage_map[event.type]
            dashboard.set_stage(stage)
            dashboard.event(msg(event) if callable(msg) else msg)
            if event.type == EventType.REMEDIATION_COMPLETE:
                _LAST_RUN["healed"] = True
            if event.type == EventType.DEPLOYMENT_COMPLETE:
                conclusion = event.data.get("conclusion", event.data.get("status", ""))
                _LAST_RUN["cicd"] = conclusion
                if event.data.get("repo_url"):
                    _LAST_RUN["repo_url"] = event.data["repo_url"]
                dashboard._cicd_status = (
                    "success" if conclusion == "success"
                    else "failed" if conclusion in ("failed", "failure")
                    else conclusion
                )
                if event.data.get("repo_url"):
                    dashboard._repo = event.data["repo_url"]
                # ── DB: persist deployment ────────────────────────────────
                import uuid as _uuid
                _db.save_deployment({
                    "id"         : str(event.data.get("run_id", _uuid.uuid4()))[:18],
                    "service"    : Path(project_path).name,
                    "repo_url"   : event.data.get("repo_url", ""),
                    "status"     : conclusion or "unknown",
                    "framework"  : "",
                    "language"   : "",
                    "files_count": 0,
                })

        await orig_pub(event)

        # Resume dashboard AFTER events that are done producing output
        if event.type in _RESUME_AFTER:
            dashboard.resume()

    orchestrator.event_bus.publish = _tracked

    # ── Start dashboard ────────────────────────────────────────────────────
    dashboard.set_stage("SCAFFOLD")
    dashboard.start()
    dashboard._draw()

    if not _run_flow:
        dashboard.set_stage("DONE")
        dashboard.event("flow skipped — chat mode")
        dashboard._draw()
        await asyncio.sleep(1)
        dashboard.stop()
        await orchestrator.stop_approval_server()
        return _db

    await orchestrator.run_scaffold(
        project_path  = project_path,
        dry_run       = False,
        skip_scaffold = _skip_scaffold,
    )
    if getattr(orchestrator, "last_repo_url", ""):
        _LAST_RUN["repo_url"] = orchestrator.last_repo_url

    dashboard.set_stage("DONE")
    dashboard.event("pipeline complete")
    dashboard._draw()
    await asyncio.sleep(1)
    dashboard.stop()
    await orchestrator.stop_approval_server()
    # close the GitHub HTTP session (removes "Unclosed client session" warnings)
    try:
        if _CICD_AVAILABLE:
            await provider.close()
    except Exception:
        pass
    # Cancel any lingering approval tasks so their input() calls don't
    # bleed into the post-pipeline menu prompt.
    for task in asyncio.all_tasks():
        if task.get_name().startswith("approval-"):
            task.cancel()
    await asyncio.sleep(0.1)  # let cancelled tasks finish cleanup
    # ── Return db so main() can use it for the history menu ───────────────
    return _db


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    project_path = str(Path.cwd())
    state_file   = Path(project_path) / ".devops_state"

    # ── Main loop — no recursion, no nested asyncio.run() ─────────────
    _rerun = False
    _auto  = False
    _auto_runs = 0
    _seen_states: set = set()          # project states produced by fixes in this loop
    # 0 = no limit (default). The loop still stops by itself when a fix makes
    # no progress, or when the problem needs a manual fix.
    try:
        MAX_AUTO_RUNS = int(os.getenv("DEVOPS_MAX_AUTO_RUNS", "0"))
    except ValueError:
        MAX_AUTO_RUNS = 0
    while True:
        _db = None
        _state_before = _project_fingerprint(project_path)
        try:
            _db = asyncio.run(_run_scaffold(rerun=_rerun, auto=_auto))
        except KeyboardInterrupt:
            print("\n  Interrupted.")
            _LAST_RUN["healed"] = False        # Ctrl+C always stops the auto loop

        try:
            state_file.write_text(
                f"deployed_at={datetime.utcnow().isoformat()}\n"
                f"project={project_path}\n"
            )
        except Exception:
            pass

        # ── Automatic loop ─────────────────────────────────────────────────
        # Keep going while every run fixes something NEW. Stop when:
        #   • CI/CD is green                        → done
        #   • nothing was fixed (manual fix needed, fix declined or failed)
        #   • the fix made no progress (same project state as before / already seen)
        #   • optional DEVOPS_MAX_AUTO_RUNS limit reached (0 = no limit)
        _state_after = _project_fingerprint(project_path)
        _progress = (_LAST_RUN["healed"] and _state_after != _state_before
                     and _state_after not in _seen_states)
        _seen_states.add(_state_before)
        _seen_states.add(_state_after)
        _limit_hit = MAX_AUTO_RUNS > 0 and _auto_runs >= MAX_AUTO_RUNS

        if _LAST_RUN["cicd"] == "success":
            if _auto_runs:
                print(f"\n  ✔ CI/CD is green after {_auto_runs} automatic re-run(s).")
            _auto_runs, _auto, _seen_states = 0, False, set()
        elif _progress and not _limit_hit:
            _auto_runs += 1
            print(f"\n  ↻ Fix applied — pushing it and re-running CI/CD automatically "
                  f"(run {_auto_runs}, Ctrl+C to stop)...")
            if _db:
                _db.close()
            _rerun, _auto = True, True
            continue
        else:
            if _LAST_RUN["healed"] and not _progress:
                print("\n  ✘ The fix didn't change anything new (same error keeps coming back)."
                      "\n    Stopping the automatic loop — check the last incident above.")
            elif _limit_hit:
                print(f"\n  ✘ Stopped after {MAX_AUTO_RUNS} automatic re-runs (DEVOPS_MAX_AUTO_RUNS).")
            elif _LAST_RUN["cicd"]:
                print("\n  ⏸ This problem needs a manual fix (see the steps above)."
                      "\n    Fix it, then choose [1] Run pipeline again.")
            _auto_runs, _auto, _seen_states = 0, False, set()

        print(f"\n{'─'*55}")
        print(f"  Pipeline complete. What would you like to do?")
        print(f"{'─'*55}")
        print(f"  [1] Run pipeline again")
        print(f"  [2] Open chat agent (manual tasks)")
        print(f"  [3] Query history  (incidents · events · solutions)")
        print(f"  [4] Exit")
        print(f"  [5] Change AI models (provider / model for each agent)")
        print(f"{'─'*55}")
        try:
            _choice = input("  Choose [1-5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            _choice = "4"

        if _choice == "1":
            if _db:
                _db.close()
            _rerun = True          # next iteration skips logo + LLM prompt
            continue               # restart the while loop cleanly

        elif _choice == "2":
            AgentController().run()
            continue               # return to menu after chat agent exits

        elif _choice == "3":
            _open_history(_db, project_path)
            continue               # return to menu after history exits

        elif _choice == "5":
            try:
                from providers.llm.llm_selector import reset_agent_choices
                reset_agent_choices()
                import agents.monitoring_agent.groq_analyzer as _ga
                _ga._provider = None
                print("\n  ✔ Saved models cleared (your API keys are kept).")
                print("    Choose [1] — you'll pick Scaffold + Monitoring now, and")
                print("    Knowledge + Self-Healing when an incident happens.")
            except Exception as e:
                print(f"  Could not reset models: {e}")
            continue

        # [4] or anything else → exit
        if _db:
            _db.close()
        break                      # exit the while loop

def _open_history(db, project_path: str) -> None:
    """
    Open the InteractiveCLI against the project's history.db.
    If db is None (pipeline was skipped), we try to reopen it from disk.
    """
    try:
        from core.interactive_cli import InteractiveCLI
    except ImportError:
        print("  [History] interactive_cli not available — skipping.")
        return

    if db is None:
        # Pipeline was skipped or crashed — reopen from disk
        try:
            db = ProjectDB.for_project(project_path)
        except Exception as exc:
            print(f"  [History] Could not open database: {exc}")
            return

    project_id = getattr(db, "project_id", Path(project_path).name)
    InteractiveCLI(db=db, project_id=project_id).run()


if __name__ == "__main__":
    main()