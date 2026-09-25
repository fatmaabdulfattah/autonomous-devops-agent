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
        ok  = ['python -m py_compile main.py', 'python -m compileall .', 'python -m py_compile "D:/p/main.py"']
        bad = ['git checkout -b fix/x', 'git push', 'docker build -t a .', 'cd "D:/p"',
               'powershell -Command "(Get-Content Dockerfile) -replace a,b"',
               "sed -i 's/a/b/' Dockerfile", 'pip install x && git push', 'rm -rf .',
               'python -m pip install fastapi==0.141.1', 'pip install -r requirements.txt', 'npm ci',
               'python -m py_compile D:/devops_test/requirements.txt', 'python -m py_compile Dockerfile']
        assert all(S._is_safe_command(c) for c in ok)
        assert not any(S._is_safe_command(c) for c in bad), [c for c in bad if S._is_safe_command(c)]
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1500:]


def test_knowledge_and_healing_models_are_chosen_lazily(tmp_path):
    """Scaffold asked at startup; Knowledge/Self-Healing asked only when they run."""
    import textwrap
    code = textwrap.dedent(f"""
        import os, asyncio, builtins, hashlib, numpy as np
        os.environ['DEVOPS_AGENT_HOME'] = r'{tmp_path}'; os.environ['QDRANT_MODE'] = 'embedded'
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import agents.knowledge_agent.shared.runtime as rt
        class F:
            def encode(self, t): return np.ones(384) / 20
        rt._encoder = F()
        import devops
        from core.orchestrator import Orchestrator
        o = Orchestrator()
        ka, sh = o.registry.get_agent('knowledge_agent'), o.registry.get_agent('self_healing_agent')
        assert ka and sh
        calls = []
        ka.run = lambda *a, **k: calls.append('ka.run') or 'ran'
        async def rem(sol): calls.append('sh.remediate'); return 'healed'
        sh.remediate = rem
        class P:
            def __init__(s, n): s.name, s.default_model = n, 'm-' + n
        asked = []
        def fake_get(agent, use_saved=False, **k):
            if use_saved: raise LookupError('none saved')
            asked.append(agent); return P('gemini')
        devops.get_llm_provider = fake_get
        answers = iter(['', 'c'])
        builtins.input = lambda *a: next(answers)
        class D:
            def pause(self): pass
            def resume(self): pass
        devops._attach_lazy_llm_choice(o, D())
        assert asked == []                                  # nothing asked at startup
        assert ka.run('err') == 'ran'                        # 1st incident: full menu
        assert asked == ['knowledge'] and ka.agent._provider.name == 'gemini'
        assert ka.run('err2') == 'ran'                       # 2nd: 'Use GEMINI?' -> Enter keeps it
        assert asked == ['knowledge']
        assert asyncio.run(sh.remediate(None)) == 'healed'   # healer: full menu on first use
        assert asked == ['knowledge', 'healing'], asked
        print('ok', calls)
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2500:] + r.stdout[-500:]


# ── git sync before push ──────────────────────────────────────────────────────
def _git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def _push_code(proj, remote):
    import textwrap
    return textwrap.dedent(f"""
        import asyncio
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from core.orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        print('PUSH', asyncio.run(o._push_to_github(r'{proj}', 'file://{remote}', 'ghp_SECRET')))
        print('URL', Orchestrator._current_remote_url(r'{proj}'))
    """)


def _teammate_commit(remote, tmp_path, fname, text):
    other = tmp_path / ("mate_" + fname.replace(".", "_"))
    _git("clone", "-q", str(remote), str(other))
    (other / fname).write_text(text)
    _git("add", ".", cwd=other)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "teammate " + fname, cwd=other)
    assert _git("push", "-q", cwd=other).returncode == 0


def test_push_syncs_teammate_commits_instead_of_failing(tmp_path):
    remote = tmp_path / "remote.git"; proj = tmp_path / "proj"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    proj.mkdir(); (proj / "app.py").write_text("x = 1\n")
    r = subprocess.run([sys.executable, "-c", _push_code(proj, remote)], capture_output=True, text=True, cwd=proj)
    assert "PUSH True" in r.stdout, r.stderr[-1500:]
    assert "ghp_SECRET" not in (proj / ".git" / "config").read_text()
    assert f"URL file://{remote}" in r.stdout                   # remembered, token-free
    _teammate_commit(remote, tmp_path, "t.txt", "teammate\n")  # GitHub now has a commit we don't
    (proj / "app.py").write_text("x = 2\n")
    r = subprocess.run([sys.executable, "-c", _push_code(proj, remote)], capture_output=True, text=True, cwd=proj)
    assert "PUSH True" in r.stdout, r.stdout + r.stderr[-1500:]
    files = _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout
    assert "t.txt" in files and "app.py" in files               # both sides kept
    assert _git("--git-dir", str(remote), "show", "main:app.py").stdout == "x = 2\n"


def test_push_to_existing_repo_with_unrelated_history(tmp_path):
    """Repo created on GitHub with a README; local project never pushed before."""
    remote = tmp_path / "remote.git"; proj = tmp_path / "proj"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    _teammate_commit(remote, tmp_path, "README.md", "# repo made on github\n")
    proj.mkdir(); (proj / "app.py").write_text("x = 1\n")
    r = subprocess.run([sys.executable, "-c", _push_code(proj, remote)], capture_output=True, text=True, cwd=proj)
    assert "PUSH True" in r.stdout, r.stdout + r.stderr[-1500:]
    files = _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout
    assert "README.md" in files and "app.py" in files


def test_push_real_conflict_stops_cleanly(tmp_path):
    remote = tmp_path / "remote.git"; proj = tmp_path / "proj"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    proj.mkdir(); (proj / "app.py").write_text("x = 1\n")
    subprocess.run([sys.executable, "-c", _push_code(proj, remote)], capture_output=True, text=True, cwd=proj)
    _teammate_commit(remote, tmp_path, "app.py", "x = 999\n")  # same line changed on GitHub
    (proj / "app.py").write_text("x = 2\n")
    r = subprocess.run([sys.executable, "-c", _push_code(proj, remote)], capture_output=True, text=True, cwd=proj)
    assert "PUSH False" in r.stdout and "merge conflict" in r.stdout, r.stdout
    assert not (proj / ".git" / "rebase-merge").exists()        # rebase undone
    assert (proj / "app.py").read_text() == "x = 2\n"           # our work untouched
    assert _git("--git-dir", str(remote), "show", "main:app.py").stdout == "x = 999\n"
    assert "ghp_SECRET" not in (proj / ".git" / "config").read_text()


def test_auto_rerun_until_cicd_success(tmp_path):
    """Loop while every run fixes something NEW; stop on success, manual fix,
    no progress (same/oscillating fix) or the optional limit."""
    import textwrap
    code = textwrap.dedent("""
        import builtins, os
        from pathlib import Path
        os.environ.pop('DEVOPS_MAX_AUTO_RUNS', None)
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import devops
        builtins.input = lambda *a: '4'
        app = Path('app.py')

        def scenario(steps):
            # steps: list of (file content written by the 'fix' or None, cicd, healed)
            it, calls = iter(steps), []
            async def fake(rerun=False, auto=False):
                calls.append(auto)
                content, cicd, healed = next(it)
                if content is not None:
                    app.write_text(content)
                devops._LAST_RUN['cicd'], devops._LAST_RUN['healed'] = cicd, healed
            devops._run_scaffold = fake
            app.write_text('start')
            devops.main()
            return calls

        # 7 different healable incidents in a row, then green -> no limit by default
        steps = [(f'fix{i}', 'failure', True) for i in range(7)] + [(None, 'success', False)]
        assert scenario(steps) == [False] + [True] * 7

        # same fix again (nothing changes) -> stop, show menu
        assert scenario([('A', 'failure', True), ('A', 'failure', True)]) == [False, True]

        # oscillation A -> B -> A -> stop
        assert scenario([('A', 'failure', True), ('B', 'failure', True),
                         ('A', 'failure', True)]) == [False, True, True]

        # manual fix needed (nothing healed) -> no automatic re-run
        assert scenario([(None, 'failure', False)]) == [False]

        # optional limit still works
        os.environ['DEVOPS_MAX_AUTO_RUNS'] = '2'
        steps = [(f'x{i}', 'failure', True) for i in range(5)]
        assert scenario(steps) == [False, True, True]
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2500:] + r.stdout[-1500:]


def test_ci_result_comes_from_the_pushed_commit_only():
    """Regression: an OLD successful run was reported as the result of a new push."""
    import textwrap
    code = textwrap.dedent("""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from core.orchestrator import Orchestrator as O
        old = {'id': 1, 'head_sha': 'aaa', 'conclusion': 'success', 'path': '.github/workflows/deploy.yml'}
        new = {'id': 2, 'head_sha': 'bbb', 'conclusion': 'failure', 'path': '.github/workflows/deploy.yml'}
        other = {'id': 3, 'head_sha': 'bbb', 'conclusion': 'success', 'path': '.github/workflows/lint.yml'}
        assert O._pick_run([old], 'bbb') is None                  # run not there yet -> keep waiting
        assert O._pick_run([old, new], 'bbb')['id'] == 2          # never the old commit's run
        assert O._pick_run([other, new], 'bbb')['id'] == 2        # prefer our deploy.yml
        assert O._pick_run([other], 'bbb')['id'] == 3
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1500:]


def test_push_records_pushed_commit(tmp_path):
    import textwrap
    remote = tmp_path / "remote.git"; proj = tmp_path / "proj"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    proj.mkdir(); (proj / "app.py").write_text("x = 1\n")
    code = textwrap.dedent(f"""
        import asyncio
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from core.orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        asyncio.run(o._push_to_github(r'{proj}', 'file://{remote}', 't'))
        print('SHA', o.last_pushed_sha)
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=proj)
    head = _git("--git-dir", str(remote), "rev-parse", "main").stdout.strip()
    assert f"SHA {head}" in r.stdout and len(head) == 40, r.stdout + r.stderr[-1000:]


def test_change_models_keeps_api_keys(tmp_path, monkeypatch):
    import textwrap, json
    monkeypatch.setenv("DEVOPS_AGENT_HOME", str(tmp_path))
    (tmp_path / "llm_config.json").write_text(json.dumps(
        {"api_keys": {"groq": "k"}, "agents": {"scaffold": {"provider": "groq", "model": "m"}}}))
    code = textwrap.dedent("""
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from providers.llm.llm_selector import reset_agent_choices, get_all_agent_configs
        reset_agent_choices(); assert get_all_agent_configs() == {}; print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1000:]
    assert json.loads((tmp_path / "llm_config.json").read_text())["api_keys"] == {"groq": "k"}


def test_repo_url_read_from_env():
    import textwrap
    code = textwrap.dedent("""
        import asyncio, os, builtins
        os.environ['GITHUB_REPO'] = 'https://github.com/me/proj.git'
        os.environ.pop('GITHUB_TOKEN', None)          # stop right after the URL step
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from core.orchestrator import Orchestrator
        from core.event_bus import Event, EventType
        builtins.input = lambda *a: (_ for _ in ()).throw(AssertionError('asked for URL'))
        o = Orchestrator(); o.auto_mode = True           # skip the approval gate
        ev = Event(type=EventType.SCAFFOLD_COMPLETE, source='t',
                   data={'generated_files': [], 'project_path': '.', 'dry_run': False})
        asyncio.run(o._on_scaffold_complete(ev))
        assert o.last_repo_url == 'https://github.com/me/proj.git'
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:] + r.stdout[-500:]


def test_noop_push_is_detected(tmp_path):
    import textwrap
    remote = tmp_path / "remote.git"; proj = tmp_path / "proj"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    proj.mkdir(); (proj / "app.py").write_text("x = 1\n")
    code = textwrap.dedent(f"""
        import asyncio
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        from core.orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        asyncio.run(o._push_to_github(r'{proj}', 'file://{remote}', 't')); print('FIRST', o.last_push_noop)
        asyncio.run(o._push_to_github(r'{proj}', 'file://{remote}', 't')); print('SECOND', o.last_push_noop)
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=proj)
    assert "FIRST False" in r.stdout and "SECOND True" in r.stdout, r.stdout + r.stderr[-1000:]


def test_rerun_when_nothing_new_was_pushed():
    """Secrets fixed on GitHub -> no new commit -> the agent must re-run the workflow,
    and report the NEW attempt's result (not the old failure)."""
    import textwrap
    code = textwrap.dedent("""
        import asyncio, os
        os.environ['GITHUB_TOKEN'] = 't'; os.environ['GITHUB_REPO'] = 'https://github.com/me/p.git'
        from auto_devops_agent import _bootstrap; _bootstrap.setup()
        import core.orchestrator as orch
        from core.orchestrator import Orchestrator
        from core.event_bus import Event, EventType
        real_sleep = asyncio.sleep
        orch.asyncio.sleep = lambda *a, **k: real_sleep(0)
        o = Orchestrator(); o.auto_mode = True
        class CICD: pass
        o.registry.register('cicd_agent', CICD()) if hasattr(o.registry, 'register') else o.register_agent('cicd_agent', CICD())
        async def push(*a, **k):
            o.last_pushed_sha, o.last_push_noop = 'abc', True
            return True
        o._push_to_github = push
        old = {'id': 7, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1, 'head_sha': 'abc'}
        async def latest(*a, **k): return old
        o._get_latest_run = latest
        reruns = []
        async def rerun(repo, run_id, token): reruns.append(run_id); return True
        o._rerun_workflow = rerun
        async def get_run(*a, **k):
            return {'id': 7, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 2}
        o._get_run = get_run
        async def logs(*a, **k): return ['ok']
        o._get_run_logs = logs
        async def nomon(): pass
        o.start_monitoring_agent = nomon
        seen = []
        async def capture(ev):
            if ev.type == EventType.DEPLOYMENT_COMPLETE: seen.append(ev.data['conclusion'])
        o.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)
        ev = Event(type=EventType.SCAFFOLD_COMPLETE, source='t',
                   data={'generated_files': [], 'project_path': '.', 'dry_run': False})
        asyncio.run(o._on_scaffold_complete(ev))
        assert reruns == [7], reruns
        assert seen == ['success'], seen
        print('ok')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-3000:] + r.stdout[-1500:]
