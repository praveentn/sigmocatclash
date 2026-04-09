#!/usr/bin/env bash
# ==============================================================
#  SigmoCatClash — Linux / macOS / Railway Launcher
#  Creates / activates .venv, installs deps, validates .env,
#  then runs the bot.
#
#  Usage:
#    bash start.sh           — full setup + run (local dev)
#    bash start.sh --setup   — setup only (Railway build step)
# ==============================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/startup.log"
SETUP_ONLY=false

# Parse flags
for arg in "$@"; do
  case "$arg" in
    --setup) SETUP_ONLY=true ;;
  esac
done

# ── Logging helper ─────────────────────────────────────────────
log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')]  $*"
  echo "$msg"
  echo "$msg" >> "$LOGFILE"
}

log "=============================================="
log " SigmoCatClash | Discord Bot Launcher"
log "=============================================="

# ── Python check ───────────────────────────────────────────────
if command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null; then
  PYTHON=python
else
  log "[ERROR] Python not found. Install Python 3.10+."
  exit 1
fi

PY_VERSION=$($PYTHON --version 2>&1)
log "[INFO]  Found $PY_VERSION"

# ── Virtual environment ────────────────────────────────────────
VENV="$ROOT/.venv"
if [ ! -f "$VENV/bin/activate" ]; then
  log "[SETUP] Creating virtual environment at .venv ..."
  $PYTHON -m venv "$VENV"
  log "[OK]    Virtual environment created."
fi

log "[INFO]  Activating virtual environment..."
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── pip upgrade ────────────────────────────────────────────────
log "[SETUP] Upgrading pip..."
pip install --upgrade pip --quiet 2>> "$LOGFILE"

# ── Dependencies ───────────────────────────────────────────────
log "[SETUP] Installing / updating dependencies..."
pip install -r "$ROOT/requirements.txt" --quiet 2>> "$LOGFILE"
log "[OK]    Dependencies installed."

# ── .env validation ────────────────────────────────────────────
# On Railway, DISCORD_TOKEN is injected as a real env var — skip file check.
if [ -z "${DISCORD_TOKEN:-}" ]; then
  if [ ! -f "$ROOT/.env" ]; then
    log "[ERROR] .env not found and DISCORD_TOKEN env var is not set!"
    log "        Copy .env.example → .env and set DISCORD_TOKEN."
    exit 1
  fi
  TOKEN_VAL=$(grep -E "^DISCORD_TOKEN=.+" "$ROOT/.env" | cut -d= -f2- | tr -d ' ' || true)
  if [ -z "$TOKEN_VAL" ]; then
    log "[ERROR] DISCORD_TOKEN is empty in .env!"
    log "        Open .env and paste your bot token."
    exit 1
  fi
  log "[OK]    DISCORD_TOKEN found in .env."
else
  log "[OK]    DISCORD_TOKEN found in environment (Railway / CI)."
fi

if $SETUP_ONLY; then
  log "[INFO]  --setup flag set — skipping bot launch (build step complete)."
  exit 0
fi

# ── Launch ─────────────────────────────────────────────────────
log "[INFO]  Starting SigmoCatClash... (Ctrl+C to stop)"
log "        Bot log: logs/bot.log"
log "=============================================="
echo ""

exec $PYTHON "$ROOT/bot.py" 2>&1 | tee -a "$LOG_DIR/bot.log"
