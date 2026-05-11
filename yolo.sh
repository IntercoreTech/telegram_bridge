#!/usr/bin/env bash
# yolo.sh — launch Claude Code with all guardrails off, via the Telegram-bridge
# pty wrapper so phone messages auto-inject into the live session.
#
# WARNING: this skips permission prompts, auto-trusts the folder, and lets
# Claude run any bash command (including sudo) without confirmation. Only
# run inside a throwaway directory, container, or VM you are willing to lose.
#
# Also: resumes the most recent session for the current dir, and snapshots
# session .jsonl files to ~/.claude/yolo-snapshots/ every 20 minutes.
#
# Wrapper resolution (first match wins):
#   1. $CLAUDE_PTY_WRAPPER (explicit override)
#   2. ~/bin/claude_pty_wrapper.py (install.sh location)
#   3. ~/Workspace/telegram_bridge/claude_pty_wrapper.py (repo clone)
# If none are found, falls back to running `claude` directly (no inject socket;
# Telegram messages will queue to phone-queue.txt instead).

set -euo pipefail

YELLOW='\033[1;33m'
RED='\033[1;31m'
RESET='\033[0m'

echo -e "${RED}=== YOLO MODE ===${RESET}"
echo -e "${YELLOW}Permissions skipped, folder auto-trusted, sudo allowed.${RESET}"
echo -e "${YELLOW}CWD: $(pwd)${RESET}"
echo

# Auto-trust the current working directory so Claude doesn't prompt.
# Claude Code stores trusted dirs in ~/.claude.json under projects[<cwd>].hasTrustDialogAccepted.
# Prefer jq if present; fall back to python3 (jq isn't installed on every box).
CLAUDE_JSON="${HOME}/.claude.json"
CWD="$(pwd)"
trust_with_jq() {
  local tmp; tmp="$(mktemp)"
  jq --arg dir "${CWD}" '
    .projects = (.projects // {}) |
    .projects[$dir] = ((.projects[$dir] // {}) + {hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true})
  ' "${CLAUDE_JSON}" > "${tmp}" && mv "${tmp}" "${CLAUDE_JSON}"
}
trust_with_python() {
  CLAUDE_JSON="${CLAUDE_JSON}" CWD="${CWD}" python3 - <<'PY'
import json, os, tempfile
path = os.environ["CLAUDE_JSON"]
cwd = os.environ["CWD"]
with open(path) as f:
    data = json.load(f)
projects = data.setdefault("projects", {})
entry = projects.setdefault(cwd, {})
entry["hasTrustDialogAccepted"] = True
entry["hasCompletedProjectOnboarding"] = True
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
PY
}
if [ -f "${CLAUDE_JSON}" ]; then
  if command -v jq >/dev/null 2>&1; then
    trust_with_jq
  elif command -v python3 >/dev/null 2>&1; then
    trust_with_python
  else
    echo -e "${YELLOW}Warning: neither jq nor python3 found — folder not auto-trusted.${RESET}"
  fi
fi

# --- Session snapshots every 20 minutes ---------------------------------------
# Claude Code persists sessions to ~/.claude/projects/<encoded-cwd>/<id>.jsonl
# (slashes in cwd → dashes). We copy them to a timestamped backup dir on a
# fixed interval so you have point-in-time snapshots, not just the live file.
ENCODED_CWD="${CWD//\//-}"
SESSION_DIR="${HOME}/.claude/projects/${ENCODED_CWD}"
SNAPSHOT_ROOT="${HOME}/.claude/yolo-snapshots/${ENCODED_CWD}"
mkdir -p "${SNAPSHOT_ROOT}"

SNAPSHOT_INTERVAL_SECONDS=1200  # 20 minutes

(
  while true; do
    sleep "${SNAPSHOT_INTERVAL_SECONDS}"
    if [ -d "${SESSION_DIR}" ]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      dest="${SNAPSHOT_ROOT}/${ts}"
      mkdir -p "${dest}"
      # Force file sync, then copy *.jsonl session logs.
      sync || true
      cp -a "${SESSION_DIR}"/*.jsonl "${dest}/" 2>/dev/null || true
    fi
  done
) &
SNAPSHOT_PID=$!

# Make sure the snapshot loop dies with this script.
trap 'kill "${SNAPSHOT_PID}" 2>/dev/null || true' EXIT INT TERM

# Decide whether to resume the most recent session for this cwd.
RESUME_FLAG=()
if [ -d "${SESSION_DIR}" ] && compgen -G "${SESSION_DIR}/*.jsonl" >/dev/null; then
  RESUME_FLAG=(--continue)
  echo -e "${YELLOW}Resuming most recent session in ${SESSION_DIR}${RESET}"
else
  echo -e "${YELLOW}No prior session for this dir — starting fresh.${RESET}"
fi
echo -e "${YELLOW}Snapshotting sessions to ${SNAPSHOT_ROOT} every 20 min.${RESET}"
echo

# Allow everything via env-level permission overrides.
export CLAUDE_CODE_AUTO_ACCEPT_TRUST=1
export IS_SANDBOX=1

# Resolve the pty wrapper. The wrapper owns /tmp/claude-inject.sock so the
# Telegram bridge daemon can push messages into the live TUI as keystrokes.
WRAPPER_CANDIDATES=(
  "${CLAUDE_PTY_WRAPPER:-}"
  "${HOME}/bin/claude_pty_wrapper.py"
  "${HOME}/Workspace/telegram_bridge/claude_pty_wrapper.py"
)
WRAPPER=""
for candidate in "${WRAPPER_CANDIDATES[@]}"; do
  if [ -n "${candidate}" ] && [ -f "${candidate}" ]; then
    WRAPPER="${candidate}"
    break
  fi
done

CLAUDE_ARGS=(
  --dangerously-skip-permissions
  --permission-mode bypassPermissions
  --allowedTools "Bash,Bash(sudo *),Bash(*),Edit,Write,Read,WebFetch,WebSearch"
  "${RESUME_FLAG[@]}"
  "$@"
)

# Run in foreground (no `exec`) so the trap fires and stops the snapshot loop.
if [ -n "${WRAPPER}" ]; then
  echo -e "${YELLOW}Using pty wrapper: ${WRAPPER}${RESET}"
  echo -e "${YELLOW}Inject socket: ${CLAUDE_INJECT_SOCK:-/tmp/claude-inject.sock}${RESET}"
  echo
  python3 "${WRAPPER}" "${CLAUDE_ARGS[@]}"
else
  echo -e "${YELLOW}Warning: no pty wrapper found — running claude directly.${RESET}"
  echo -e "${YELLOW}Telegram messages will queue to ~/Workspace/phone-queue.txt.${RESET}"
  echo
  claude "${CLAUDE_ARGS[@]}"
fi
