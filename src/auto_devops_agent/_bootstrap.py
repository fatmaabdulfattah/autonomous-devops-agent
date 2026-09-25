"""
Runtime path bootstrap.

The agent code base uses short top-level imports (``core``, ``agents``,
``providers``, ``models`` ...) that were written for running from a repo
checkout. Instead of rewriting hundreds of imports, the installed package
recreates that repo layout on ``sys.path`` at startup — exactly what the old
``devops.py`` did with ``sys.path.insert``.

Order matters: ``devops_agent/`` must win over the package root so that
``models`` / ``config`` resolve to ``devops_agent/models`` / ``devops_agent/config.py``,
matching the original dev behaviour.
"""
import os
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
DEVOPS_AGENT_DIR = PKG_DIR / "devops_agent"

# User-level home for config/state (never write into site-packages)
DEVOPS_HOME = Path(os.getenv("DEVOPS_AGENT_HOME", Path.home() / ".devops_agent"))


def setup() -> None:
    for p in (str(PKG_DIR), str(DEVOPS_AGENT_DIR)):
        if p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, str(PKG_DIR))
    sys.path.insert(0, str(DEVOPS_AGENT_DIR))

    # .env resolution: project dir (cwd, walking up) first, then ~/.devops_agent/.env.
    # load_dotenv never overrides already-set variables, so the first hit wins.
    try:
        from dotenv import load_dotenv, find_dotenv
        project_env = find_dotenv(usecwd=True)
        if project_env:
            load_dotenv(project_env)
        home_env = DEVOPS_HOME / ".env"
        if home_env.exists():
            load_dotenv(home_env)
    except ImportError:
        pass
