"""
shared/runtime.py
-----------------
One place that creates the heavy Knowledge-agent resources, cached per process.

Qdrant:
  * If a Qdrant server answers at QDRANT_HOST:QDRANT_PORT (docker / compose) → use it.
  * Otherwise → embedded Qdrant stored on disk (~/.devops_agent/qdrant). No Docker needed.
    Embedded mode allows ONE client per folder, hence the singleton.

Embeddings (all-MiniLM-L6-v2, 384-dim, same model as before):
  * fastembed (ONNX, ~50 MB, no torch)        — default
  * sentence-transformers (torch)             — used if installed and fastembed isn't
"""
import atexit
import os
import threading
import urllib.request
from pathlib import Path

_lock = threading.Lock()
_client = None
_encoder = None
_mode = ""

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _server_up(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/readyz", timeout=2):
            return True
    except Exception:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}", timeout=2):
                return True
        except Exception:
            return False


def get_qdrant_client(config=None):
    """Shared QdrantClient (server if reachable, else embedded on disk)."""
    global _client, _mode
    with _lock:
        if _client is not None:
            return _client
        from qdrant_client import QdrantClient
        host = getattr(config, "qdrant_host", None) or os.getenv("QDRANT_HOST", "localhost")
        port = int(getattr(config, "qdrant_port", None) or os.getenv("QDRANT_PORT", "6333"))
        if os.getenv("QDRANT_MODE", "auto") != "embedded" and _server_up(host, port):
            _client, _mode = QdrantClient(host=host, port=port), f"server {host}:{port}"
        else:
            home = Path(os.getenv("DEVOPS_AGENT_HOME", Path.home() / ".devops_agent"))
            path = home / "qdrant"
            path.mkdir(parents=True, exist_ok=True)
            _client, _mode = QdrantClient(path=str(path)), f"embedded {path}"
        atexit.register(_close)
        return _client


def _close():
    try:
        if _client is not None:
            _client.close()
    except Exception:
        pass


def qdrant_mode() -> str:
    return _mode


class _Encoder:
    """Minimal .encode(text) -> numpy 1-D array, like SentenceTransformer."""

    def __init__(self):
        try:
            from fastembed import TextEmbedding
            cache = Path(os.getenv("DEVOPS_AGENT_HOME", Path.home() / ".devops_agent")) / "models"
            self._fe = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache))
            self._st = None
        except ImportError:
            from sentence_transformers import SentenceTransformer
            self._fe = None
            self._st = SentenceTransformer(MODEL_NAME)

    def encode(self, text):
        if self._fe is not None:
            import numpy as np
            if isinstance(text, str):
                return np.asarray(next(iter(self._fe.embed([text]))))
            return np.asarray(list(self._fe.embed(list(text))))
        return self._st.encode(text)


def get_encoder() -> _Encoder:
    global _encoder
    with _lock:
        if _encoder is None:
            _encoder = _Encoder()
        return _encoder
