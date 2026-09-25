"""Smoke tests for the installed package. Run from OUTSIDE the repo root:
    cd /tmp && pytest /path/to/tests/test_packaging.py
"""
import subprocess
import sys


def _run(*args):
    return subprocess.run([sys.executable, "-m", "auto_devops_agent.cli", *args],
                          capture_output=True, text=True, timeout=120)


def test_version():
    r = _run("--version")
    assert r.returncode == 0 and "0.1.0" in r.stdout


def test_cli_import_chain_without_keys(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    code = "from auto_devops_agent import _bootstrap; _bootstrap.setup(); import devops; print('ok')"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_package_data_shipped():
    import auto_devops_agent, pathlib
    data = pathlib.Path(auto_devops_agent.__file__).parent / "agents/knowledge_agent/data"
    assert (data / "knowledge_graph.json").exists()


def test_scaffold_uses_injected_provider():
    from auto_devops_agent import _bootstrap
    _bootstrap.setup()
    from agents.scaffold_agent.shared.config import load_config
    from agents.scaffold_agent.core_scaffold.scaffold_agent import ScaffoldAgent
    from providers.llm.base_llm_provider import LLMResponse

    class Fake:
        name = "fake"
        def chat(self, messages, model=None, **kw):
            return LLMResponse("OUT", "m", "fake")

    agent = ScaffoldAgent(load_config())
    agent.set_llm_provider(Fake())
    assert agent._call_llm("x") == "OUT"


def test_llm_selector_enabled_and_reuses_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVOPS_AGENT_HOME", str(tmp_path))
    code = (
        "from auto_devops_agent import _bootstrap; _bootstrap.setup();"
        "import json, pathlib, os;"
        "pathlib.Path(os.environ['DEVOPS_AGENT_HOME'], 'llm_config.json').write_text(json.dumps("
        "{'api_keys': {'groq': 'k'}, 'agents': {'scaffold': {'provider': 'groq', 'model': 'm1'}}}));"
        "import devops; assert devops._LLM_SELECTOR_AVAILABLE, 'selector disabled';"
        "from providers.llm.llm_selector import get_llm_provider, get_all_agent_configs;"
        "p = get_llm_provider(agent='scaffold', use_saved=True);"
        "assert p.name == 'groq' and p.default_model == 'm1', p;"
        "assert 'scaffold' in get_all_agent_configs(); print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout


def test_events_handled_once_after_provider_wrap():
    """Regression: scaffold/incident/investigation handlers must not run twice."""
    code = (
        "from auto_devops_agent import _bootstrap; _bootstrap.setup();"
        "import devops; from core.orchestrator import Orchestrator; from core.event_bus import EventType;"
        "o = Orchestrator(); devops._attach_llm_providers_to_orchestrator(o, None, {'scaffold': object()});"
        "subs = o.event_bus._subscribers;"
        "n = [len(subs.get(e, [])) for e in (EventType.SCAFFOLD_STARTED, EventType.INCIDENT_CREATED, EventType.INVESTIGATION_COMPLETE)];"
        "assert n == [1, 1, 1], n; print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout


def test_syntax_heal_falls_back_to_detector_files(tmp_path):
    """Regression: LLM returned empty files_to_fix -> healing silently skipped."""
    (tmp_path / "main.py").write_text("def health()\n    return 1\n")
    code = (
        "from auto_devops_agent import _bootstrap; _bootstrap.setup();"
        "from core.orchestrator import Orchestrator;"
        "d = {'all_flawed_files': ['main.py:6'],"
        "     'syntax_errors': [{'file': '/home/runner/work/p/p/main.py', 'line': 6, 'raw_message': 'expected :'}]};"
        "r = Orchestrator._files_from_detector(d);"
        "assert len(r) == 1 and r[0]['file'].endswith('main.py') and r[0]['line'] == 6, r; print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout


def test_knowledge_agent_registers_with_embedded_qdrant(tmp_path):
    """Knowledge agent must work with the plain install: no Docker, no Qdrant server.
    Uses a stand-in encoder so the test needs no model download."""
    import textwrap
    code = textwrap.dedent(f"""
        import os, hashlib, numpy as np
        os.environ['DEVOPS_AGENT_HOME'] = r'{tmp_path}'
        os.environ['QDRANT_MODE'] = 'embedded'
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import agents.knowledge_agent.shared.runtime as rt

        class F:
            def encode(self, t):
                v = np.zeros(384)
                for w in str(t).split():
                    v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1
                return v / (np.linalg.norm(v) or 1)

        rt._encoder = F()
        from core.orchestrator import Orchestrator
        o = Orchestrator()
        assert o.registry.get_agent('knowledge_agent') is not None, 'knowledge not registered'
        assert rt.qdrant_mode().startswith('embedded'), rt.qdrant_mode()
        assert rt.get_qdrant_client().count(collection_name='devops_knowledge').count > 0
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:] + r.stdout[-500:]


def test_self_healing_uses_chosen_provider_not_hardcoded_model():
    """Regression: fixer called hard-coded qwen/qwen3.6-27b regardless of user choice."""
    import textwrap
    code = textwrap.dedent("""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import core.orchestrator  # real app import order
        from providers.llm.base_llm_provider import LLMResponse
        from agents.self_healing_agent.self_healing_agent import SelfHealingAgent
        from agents.self_healing_agent import llm_backend
        seen = {}
        class P:
            name = 'openai'; default_model = 'chosen-model'
            def chat(self, messages, model=None, **kw):
                seen['called'] = True
                return LLMResponse('<think>x</think>FIXED', 'chosen-model', 'openai')
        SelfHealingAgent.set_llm_provider(None, P())
        r = llm_backend.create(default_model='qwen/qwen3.6-27b',
                               messages=[{'role': 'user', 'content': 'hi'}], max_tokens=10,
                               reasoning_effort='none')
        assert seen.get('called') and r.choices[0].message.content == 'FIXED'
        assert llm_backend.model_name('qwen/qwen3.6-27b') == 'openai / chosen-model'
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:]


def test_healing_strips_fences_and_rejects_invalid_python(tmp_path):
    """Regression: fixer wrote ```python fences into main.py, breaking it again."""
    import textwrap
    good = tmp_path / "good.py"; good.write_text("def f()\n    return 1\n")
    bad = tmp_path / "bad.py"; bad.write_text("x = 1\n")
    code = textwrap.dedent(f"""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import core.orchestrator
        from agents.self_healing_agent.self_healing_agent import SelfHealingAgent
        from agents.self_healing_agent.models import FileModificationResult
        fenced = "```python\\ndef f():\\n    return 1\\n```"
        a = SelfHealingAgent.__new__(SelfHealingAgent)
        from pathlib import Path; a._project_root = Path(r'{tmp_path}')
        mods = a._build_modifications(
            [{{"path": r'{good}', "new_content": fenced, "action": "overwrite"}},
             {{"path": r'{bad}',  "new_content": "def broken(\\n", "action": "overwrite"}}],
            {{r'{good}': "def f()\\n    return 1\\n", r'{bad}': "x = 1\\n"}})
        a._apply_to_disk(mods)
        assert open(r'{good}').read() == "def f():\\n    return 1\\n", repr(open(r'{good}').read())
        assert open(r'{bad}').read() == "x = 1\\n"          # invalid fix rejected
        assert mods[1].error and not mods[1].applied
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:] + r.stdout[-800:]


def test_healing_finds_file_named_in_diagnosis(tmp_path):
    """Regression: pip error had no file path -> 'files_to_modify is empty', though the
    Knowledge agent's diagnosis clearly named requirements.txt."""
    import textwrap
    (tmp_path / "requirements.txt").write_text("fastapi==9.9.9\n")
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / ".venv").mkdir(); (tmp_path / ".venv" / "requirements.txt").write_text("")
    code = textwrap.dedent(f"""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import core.orchestrator
        from pathlib import Path
        from agents.self_healing_agent.self_healing_agent import SelfHealingAgent
        from agents.self_healing_agent.models import Solution
        a = SelfHealingAgent.__new__(SelfHealingAgent); a._project_root = Path(r'{tmp_path}')
        sol = Solution(incident_id='I', confidence=0.7,
            root_cause='The requirements.txt file pins fastapi==9.9.9 which does not exist',
            healing_prompt='Replace fastapi==9.9.9 with fastapi==0.141.1 in requirements.txt',
            suggested_commands=['git add requirements.txt'])
        errs = a._guard_solution(sol)
        names = [Path(f.path).name for f in sol.files_to_modify]
        assert not any('empty' in e for e in errs), errs
        assert names == ['requirements.txt'], names
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:] + r.stdout[-800:]


def test_healing_prints_diff(tmp_path):
    import textwrap
    f = tmp_path / "Dockerfile"; f.write_text("FROM python:3.11-sliimm\nCOPY requirement.txt .\n")
    code = textwrap.dedent(f"""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import core.orchestrator
        from pathlib import Path
        from agents.self_healing_agent.self_healing_agent import SelfHealingAgent
        a = SelfHealingAgent.__new__(SelfHealingAgent); a._project_root = Path(r'{tmp_path}')
        mods = SelfHealingAgent._build_modifications(
            [{{"path": r'{f}', "new_content": "FROM python:3.11-slim\\nCOPY requirements.txt .\\n", "action": "overwrite"}}],
            {{r'{f}': open(r'{f}').read()}})
        a._apply_to_disk(mods)
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr[-1500:]
    assert "4 line(s) changed" in r.stdout and "+FROM python:3.11-slim" in r.stdout, r.stdout


def test_gemini_client_stays_open():
    """Regression: throwaway genai.Client was GC'd mid-request -> 'client has been closed'."""
    import textwrap
    code = textwrap.dedent("""
        import gc
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from providers.llm.gemini_provider import GeminiProvider
        g = GeminiProvider(api_key='x')
        m = g._client().models; gc.collect()
        assert not m._api_client._httpx_client.is_closed
        assert g._client() is g._client()
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1500:]


def test_gemini_retries_then_falls_back_on_503():
    import textwrap
    code = textwrap.dedent("""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from providers.llm.gemini_provider import GeminiProvider
        GeminiProvider.RETRY_WAITS = (0, 0)
        calls = []
        class Busy(Exception):
            code = 503
        class M:
            def generate_content(self, model, contents, config):
                calls.append(model)
                if model == 'gemini-3.8-flash':
                    raise Busy('overloaded')
                class R: text = 'ok from ' + model
                return R()
            def list(self):
                class X:
                    def __init__(s, n): s.name = 'models/' + n; s.supported_actions = ['generateContent']
                return [X(n) for n in ['gemini-2.5-flash', 'gemini-3.8-flash', 'text-embedding-004',
                                        'gemini-3.5-flash-lite', 'gemini-3.1-flash-image']]
        class C: models = M()
        g = GeminiProvider(api_key='x', model='gemini-3.8-flash'); g._client_obj = C()
        r = g.chat([{'role': 'user', 'content': 'hi'}])
        assert r.content == 'ok from gemini-3.5-flash-lite', r.content
        assert calls.count('gemini-3.8-flash') == 3, calls
        lst = g.list_models()
        assert lst == ['gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-2.5-flash'], lst
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1500:] + r.stdout[-500:]


def test_healer_only_runs_safe_commands():
    import textwrap
    code = textwrap.dedent("""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import core.orchestrator
        from agents.self_healing_agent.self_healing_agent import SelfHealingAgent as S
        ok  = ['python -m py_compile main.py', 'python -m compileall .']
        bad = ['git checkout -b fix/x', 'git push', 'docker build -t a .', 'cd "D:/p"',
               'powershell -Command "(Get-Content Dockerfile) -replace a,b"',
               "sed -i 's/a/b/' Dockerfile", 'pip install x && git push', 'rm -rf .',
               'python -m pip install fastapi==0.141.1', 'pip install -r requirements.txt', 'npm ci']
        assert all(S._is_safe_command(c) for c in ok)
        assert not any(S._is_safe_command(c) for c in bad), [c for c in bad if S._is_safe_command(c)]
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1500:]


def test_push_leaves_no_token_and_does_not_force(tmp_path):
    import textwrap
    remote = tmp_path / "remote.git"; proj = tmp_path / "proj"; other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    proj.mkdir(); (proj / "app.py").write_text("x = 1\n")
    code = textwrap.dedent(f"""
        import asyncio
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from core.orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        print('push1', asyncio.run(o._push_to_github(r'{proj}', 'file://{remote}', 'ghp_SECRET')))
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=proj)
    assert "push1 True" in r.stdout, r.stderr[-1500:] + r.stdout
    cfg = (proj / ".git" / "config").read_text()
    assert "ghp_SECRET" not in cfg, cfg
    # a teammate pushes; our next push must be rejected, not force-overwrite theirs
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "t.txt").write_text("teammate\n")
    for c in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "t"], ["push", "-q"]):
        subprocess.run(["git", "-C", str(other), *c], check=True)
    (proj / "app.py").write_text("x = 2\n")
    r = subprocess.run([sys.executable, "-c", code.replace("push1", "push2")], capture_output=True, text=True, cwd=proj)
    assert "push2 False" in r.stdout, r.stdout
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
    assert " t" in log, log      # teammate's commit survived
