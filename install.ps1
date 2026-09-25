# Autonomous DevOps Agent — installer (Windows PowerShell)
#   .\install.ps1            -> installs everything (all 5 agents, all providers)
$ErrorActionPreference = "Stop"
$Pkg = "autonomous-devops-agent"
$Model = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "llama3.2:3b" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git not found: https://git-scm.com" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python 3.10+ not found: https://python.org" }

$Home2 = if ($env:DEVOPS_AGENT_HOME) { $env:DEVOPS_AGENT_HOME } else { "$HOME\.devops_agent" }
$Venv = "$Home2\venv"
if (-not (Test-Path $Venv)) { py -3 -m venv $Venv }
& "$Venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$Venv\Scripts\pip.exe" install -q $Pkg
if (Get-Command ollama -ErrorAction SilentlyContinue) { ollama pull $Model } else { Write-Host "Ollama not installed - fine if you use a cloud LLM" }

# put devops on the user PATH
$bin = "$Venv\Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") { [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User"); Write-Host "Added $bin to PATH (open a new terminal)" }

if (-not (Test-Path "$Home2\.env")) { "GROQ_API_KEY=`nGITHUB_TOKEN=" | Out-File -Encoding utf8 "$Home2\.env"; Write-Warning "Put your keys in $Home2\.env" }
& "$bin\devops.exe" --doctor
Write-Host "`nDone. cd into your project and run: devops"
