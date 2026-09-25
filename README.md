# Autonomous DevOps Agent

<!-- badges (post-release): PyPI version · CI -->

**Point it at any project folder. It writes your Docker / GitHub Actions / Kubernetes files, pushes to GitHub, watches the pipeline, and when something breaks it finds the cause and fixes it — asking for your approval before every step.**

```
devops  (run inside your project folder)
   │
   ├── 1. Scaffold     → Dockerfile, docker-compose, GitHub Actions, k8s manifests
   ├── 2. CI/CD        → pushes to GitHub, watches the Actions run
   ├── 3. Monitoring   → reads the CI logs, finds what failed
   ├── 4. Knowledge    → investigates the cause (knowledge base + web search)
   └── 5. Self-Healing → edits the broken file, shows you the diff, next run verifies it
```

Works with **Groq, Gemini, OpenAI, Claude** (cloud) or **Ollama** (local). Tested on Windows.

---

## ⚡ Quick start (5 minutes)

```bash
pip install autonomous-devops-agent
cd path/to/your/project        # a folder with your code (and a requirements.txt for Python)
devops --doctor                # checks everything; fix any [FAIL]
devops                         # start
```

Before the first run you need **two keys** in a `.env` file inside your project folder (see [Keys](#-keys)) and **two secrets** in your GitHub repo (see [GitHub setup](#-github-setup-once-per-repo)).

> **Windows tip:** if you installed inside a virtual environment, you must activate it in **every new terminal** before `devops` works:
> `C:\path\to\.venv\Scripts\activate` — otherwise you get *"'devops' is not recognized"*.

---

## 📦 Install

Needs **Python 3.10+** and **git**. About 450 MB. 4 GB RAM is enough with a cloud model.

**Recommended (isolated):**
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install autonomous-devops-agent
```

**Or the one-command installer** from this repo (creates its own environment and adds `devops` to your PATH):
```bash
./install.sh          # macOS / Linux
.\install.ps1         # Windows PowerShell
```

The first run downloads a small search model (~90 MB) once. Everything else runs inside the package — no Docker or database server needed.

---

## 🔑 Keys

Create a file named **`.env`** in your project folder (or once in `~/.devops_agent/.env` to use it for all projects):

```env
# Pick at least ONE model provider
GROQ_API_KEY=gsk_...          # free: https://console.groq.com → API Keys
GEMINI_API_KEY=...            # free: https://aistudio.google.com/apikey
OPENAI_API_KEY=sk-...         # https://platform.openai.com/api-keys
ANTHROPIC_API_KEY=sk-ant-...  # https://console.anthropic.com

# Required for pushing to GitHub
GITHUB_TOKEN=ghp_...
GITHUB_REPO=https://github.com/you/your-project.git   # optional — otherwise you're asked once
```

**GitHub token:** github.com → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate → tick **`repo`** and **`workflow`**.

> 🔒 Your `.env` never leaves your computer. Before every push the agent adds `.env` to your project's `.gitignore`, and it never stores your token in `.git/config`.
> Never paste your `.env` into chats, issues or screenshots.

**Ollama (optional, free, local):** install from https://ollama.com, then `ollama pull llama3.2:3b`, and pick **[1] Ollama** in the menu. Needs 8 GB+ RAM.

---

## 🐙 GitHub setup (once per repo)

1. **Create a repo** for your project on github.com (empty: no README, no .gitignore, no license). You'll paste its URL when `devops` asks for it.

The generated pipeline builds your Docker image and pushes it to Docker Hub, so GitHub also needs your Docker Hub login:

2. Create a free account at https://hub.docker.com → Account settings → **Personal access tokens** → create one.
3. In that GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `DOCKER_USERNAME` = your Docker Hub username
   - `DOCKER_PASSWORD` = the token from step 1

Without these, the first pipeline fails at *docker login* — the agent will tell you, but it can't fix secrets for you. After you add them, choose **[1] Run pipeline again**: since no file changed, the agent asks GitHub to re-run the workflow for you.

---

## ▶️ What happens when you run `devops`

| You see | What to answer |
|---|---|
| **LLM SETUP** at start (Scaffold, Monitoring) | pick a provider (e.g. `2` Groq or `5` Gemini), `1` to use the saved key, then a model. Choices are remembered. |
| **Knowledge / Self-Healing model** — only when an incident happens | first time: pick a provider + model. Later: `Use GEMINI / …?` → **Enter** keeps it, `c` lets you switch. |
| **Re-generate scaffold files?** (2nd run on) | `no` to keep your files, `yes` to recreate them |
| **APPROVAL REQUIRED** | `yes` / `no`. Nothing is pushed or changed without this. |
| **GitHub repo URL** | not asked if `GITHUB_REPO` is in `.env`. Otherwise: paste it the first time, later **Enter** reuses it |
| **✎ file — N line(s) changed** | the exact diff of every fix (red = removed, green = added) |
| **CI/CD: success** | done 🎉 |

**What the agent does to your repo:** it commits and pushes to `main`. If GitHub has newer commits (a teammate, or a repo created with a README), it first puts your commits on top of theirs — nothing on GitHub is ever deleted, and it never force-pushes. Only if the *same lines* were changed on both sides does it stop and ask you to resolve the conflict once. Fixes are backed up in `.self_healing_backups/`.

**Automatic re-runs:** after a fix is applied, the agent pushes it and re-runs CI/CD by itself — incident after incident — until the pipeline is green. It stops and shows the menu when a problem needs **you** (e.g. Docker Hub secrets, an expired token), when you decline a fix, or when a fix changes nothing new (the same error keeps coming back). You still approve each investigation and fix; `Ctrl+C` stops the loop. Optional hard limit: `DEVOPS_MAX_AUTO_RUNS=10` in `.env`. The repo URL is remembered, so you only type it once.

**Good models to start with:**

| Provider | Model |
|---|---|
| Groq | `openai/gpt-oss-120b` or `qwen/qwen3.8-27b` |
| Gemini | `gemini-3.5-flash-lite` (fast, generous free tier) or `gemini-3.8-flash` |

---

## ⚠️ Good to know

- **One error per run.** GitHub stops at the first failing step, so if your project has 3 bugs the agent fixes them over 3 runs — automatically, one after another. Bugs in the *same file* are usually fixed together.
- **What the pipeline checks:** Python syntax (`compileall`) and the Docker build (including `pip install`). It does **not** start your app, so bugs that only appear at runtime aren't detected yet.
- **Free-tier limits:** `429` (rate limit) and Gemini `503` (busy) are retried automatically; Gemini falls back to a lighter model if busy.

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| `'devops' is not recognized` | activate your venv (see Windows tip above) |
| `devops --doctor` shows `[FAIL]` | follow the line — usually a missing key in `.env` |
| `401 Invalid API Key` | the key is wrong or was deleted — create a new one and pick *Enter new token* |
| `model ... does not exist` / `404` | the provider retired that model — pick another from the list |
| `429` / `503` / "high demand" | free-tier limit or busy servers — wait a minute and rerun |
| pipeline fails at `docker login` | add the Docker Hub secrets (see GitHub setup) |
| `merge conflict` when pushing | the same lines changed on GitHub and locally: run `git pull --rebase origin main`, fix the conflict, `git add .` + `git rebase --continue`, then `devops` again |
| `knowledge_agent` shows `○` | first run needs internet to download the search model; or another `devops` is already running — close it |
| want to change provider / model | menu **[5] Change AI models** (keys are kept), or press `c` when asked at an incident |
| `429 ... OTPM Limit 1000` (Groq) | that model's free tier is too small for big fixes — use `openai/gpt-oss-120b` or Gemini |
| want to reset the knowledge base | delete `~/.devops_agent/qdrant/` |

---

## 🗂️ Where things are stored

| What | Where |
|---|---|
| Saved model choices + keys you entered | `~/.devops_agent/llm_config.json` (change with `DEVOPS_AGENT_HOME`) |
| Knowledge base | `~/.devops_agent/qdrant/` |
| Search model cache | `~/.devops_agent/models/` |
| Per-project history | `<project>/.devops/history.db` |
| Backups of healed files | `<project>/.self_healing_backups/` |

---

## Email approvals & alerts (optional)

The alert agent sends email notifications and handles approvals via clickable email links.
Approvals appear on both your terminal (CLI) and in email simultaneously — first response wins.

**Two routing lanes:**
- **Normal** (LOW / MEDIUM severity) → email goes to `ALERT_ENGINEER_EMAIL` only
- **Emergency** (HIGH / CRITICAL) → email goes to the full team (`ALERT_TEAM_EMAILS`) simultaneously

---

#### Step 1 — Configure Gmail

1. Google Account → **Security** → enable **2-Step Verification**
2. Go to **App Passwords** → generate one for "Mail" → copy the 16-character password

Add to `.env`:

```env
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USERNAME=you@gmail.com
ALERT_SMTP_PASSWORD=xxxx xxxx xxxx xxxx
ALERT_FROM_ADDRESS=DevOps Agent <you@gmail.com>
ALERT_ENGINEER_EMAIL=lead@company.com
ALERT_TEAM_EMAILS=alice@company.com,bob@company.com,carol@company.com
```

> `ALERT_TEAM_EMAILS` is a comma-separated list. Leave it blank to disable team broadcasts.

---

#### Step 2 — Install Cloudflare Tunnel (Windows)

The approval server needs a public URL so email links work from any device.
We use **cloudflared** (free, no account required).

**Install (one-time):**

1. Download the Windows binary:
   ```
   https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
   ```
2. Rename to `cloudflared.exe`
3. Move it to `C:\Windows\System32\` so it's available from any terminal

**Verify it works:**
```bash
cloudflared --version
```
You should see output like `cloudflared version 2024.x.x`.

**Linux / macOS:**
```bash
# macOS
brew install cloudflared

# Linux (Debian/Ubuntu)
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
```

---

#### Step 3 — Run

No separate server needed. The approval server starts automatically when you run `devops`.
At startup you'll see:

```
  [Email] approvals/alerts → lead@company.com  |  emergency team → 3 address(es)
  [ApprovalServer] 🌐 Public URL: https://xxxx.trycloudflare.com
```

When an approval gate is reached, an email is sent to the engineer with **✅ Approve** and
**❌ Deny** buttons. You can also type `yes` or `no` directly in the terminal — first response wins.

---

#### Dashboard status

When email is configured, the dashboard shows:

```
● alerting_agent           IDLE (email)
```

When email is not configured:

```
○ alerting_agent           —
```

---

#### No cloudflared?

If `cloudflared` is not installed, the approval server falls back to `localhost` only —
email links will only work if you open them on the **same machine** running the agent.
CLI approval always works regardless.

---

## 🐳 Docker Compose (experimental)

Runs everything (agent + Ollama + Qdrant) in containers — only Docker needed. Not yet tested end-to-end; use the pip install above if you're unsure.

```bash
cp .env.template .env            # fill in your keys
docker compose up -d             # starts Qdrant + Ollama, pulls llama3.2:3b once
PROJECT_DIR=/path/to/your/project docker compose run --rm devops
```
Windows PowerShell: `$env:PROJECT_DIR="C:\path\to\project"; docker compose run --rm devops`

---

## 👩‍💻 Development

```bash
git clone https://github.com/fatmaabdulfattah/autonomous-devops-agent.git
cd autonomous-devops-agent
pip install -e ".[dev]"          # editable install
pytest tests/test_packaging.py
```

Upgrade: `pip install -U autonomous-devops-agent`

<details>
<summary>Project structure</summary>

```
autonomous-devops-agent/
├── pyproject.toml              ← package metadata, dependencies, `devops` command
├── install.sh / install.ps1
├── Dockerfile / docker-compose.yml
└── src/auto_devops_agent/
    ├── cli.py                  ← `devops` entry point (--version, --doctor)
    ├── devops_agent/           ← CLI app (pipeline, chat agent)
    ├── core/                   ← orchestrator, event bus, approvals, project DB
    ├── agents/                 ← scaffold · cicd · monitoring · knowledge · self_healing
    └── providers/              ← LLM providers + GitHub provider
```
</details>

## License

MIT
