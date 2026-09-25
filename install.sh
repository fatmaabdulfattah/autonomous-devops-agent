#!/usr/bin/env bash
# Autonomous DevOps Agent — installer (macOS / Linux / WSL)
#   ./install.sh            -> installs everything (all 5 agents, all providers)
set -euo pipefail
PKG="autonomous-devops-agent"
MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
ok(){ printf "  \033[32m✔\033[0m %s\n" "$1"; }
warn(){ printf "  \033[33m!\033[0m %s\n" "$1"; }
die(){ printf "  \033[31m✘\033[0m %s\n" "$1"; exit 1; }

echo "== Autonomous DevOps Agent installer =="

command -v git >/dev/null || die "git not found — install it first: https://git-scm.com"
ok "git"

PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)'; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "Python 3.10+ not found"
ok "python: $($PY --version)"

VENV="${DEVOPS_AGENT_HOME:-$HOME/.devops_agent}/venv"
if [ ! -d "$VENV" ]; then "$PY" -m venv "$VENV"; fi
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install -q --upgrade pip

pip install -q "$PKG"
ok "installed $PKG into $VENV"

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/devops" "$HOME/.local/bin/devops"
case ":$PATH:" in *":$HOME/.local/bin:"*) ok "devops is on PATH";;
  *) warn "add to your shell profile:  export PATH=\"\$HOME/.local/bin:\$PATH\"";; esac

if command -v ollama >/dev/null; then
  ollama pull "$MODEL" && ok "Ollama model $MODEL ready (optional local LLM)"
else
  warn "Ollama not installed — fine if you use Groq/OpenAI/Claude/Gemini"
fi

ENVF="${DEVOPS_AGENT_HOME:-$HOME/.devops_agent}/.env"
if [ ! -f "$ENVF" ]; then
  printf "GROQ_API_KEY=\nGITHUB_TOKEN=\n" > "$ENVF"; chmod 600 "$ENVF"
  warn "put your keys in $ENVF (used when a project has no .env)"
fi

echo
"$VENV/bin/devops" --doctor || true
echo
echo "Done. cd into your project and run:  devops"
