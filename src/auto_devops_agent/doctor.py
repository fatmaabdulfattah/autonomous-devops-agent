"""``devops --doctor`` — quick environment check before a real run."""
import importlib.util
import os
import shutil
import urllib.request


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except Exception:
        return False


def run() -> int:
    ollama = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    if not ollama.startswith("http"):
        ollama = "http://" + ollama
    qdrant = f"http://{os.getenv('QDRANT_HOST', 'localhost')}:{os.getenv('QDRANT_PORT', '6333')}"

    checks = [
        ("git installed", shutil.which("git") is not None, True),
        ("GROQ_API_KEY set", bool(os.getenv("GROQ_API_KEY")), True),
        ("GITHUB_TOKEN set", bool(os.getenv("GITHUB_TOKEN")), False),
        (f"Ollama reachable ({ollama}) — only if you pick Ollama", _http_ok(ollama), False),
        ("docker CLI installed", shutil.which("docker") is not None, False),
        ("Knowledge agent packages (qdrant-client, fastembed)",
         all(importlib.util.find_spec(m) for m in ("qdrant_client", "fastembed")), True),
        (f"Qdrant server ({qdrant}) — optional, embedded mode used otherwise",
         _http_ok(qdrant), False),
        ("cloudflared installed (email approvals)", shutil.which("cloudflared") is not None, False),
    ]
    failed_required = 0
    for label, ok, required in checks:
        mark = "OK  " if ok else ("FAIL" if required else "warn")
        print(f"  [{mark}] {label}")
        if not ok and required:
            failed_required += 1
    print()
    print("  Ready." if not failed_required else f"  {failed_required} required check(s) failed.")
    return 1 if failed_required else 0
