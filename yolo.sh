#!/usr/bin/env bash
# yolo.sh — Claude Code in danger mode, resume last session, snapshot session every 20 min.
#
# Usage:  ./yolo.sh [extra claude args...]
# Snapshots land in ~/.claude/yolo-snapshots/
#
# This script execs the pty wrapper that owns /tmp/claude-inject.sock so the
# Telegram bridge can auto-inject prompts.

set -u

cd "$HOME/Workspace" || exit 1

SNAPSHOT_DIR="$HOME/.claude/yolo-snapshots"
mkdir -p "$SNAPSHOT_DIR"

# Map cwd to Claude Code's project directory naming: /home/x/foo -> -home-x-foo
project_dir_for_cwd() {
  printf '%s' "$HOME/.claude/projects/$(pwd | sed 's|/|-|g')"
}

# Background loop: copy the most-recently-modified session JSONL every 20 min.
(
  while true; do
    sleep 1200
    PROJECT_DIR="$(project_dir_for_cwd)"
    if [ -d "$PROJECT_DIR" ]; then
      LATEST="$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1)"
      if [ -n "${LATEST:-}" ]; then
        TS="$(date +%Y%m%d-%H%M%S)"
        cp -p "$LATEST" "$SNAPSHOT_DIR/$(basename "$LATEST" .jsonl).$TS.jsonl"
      fi
    fi
  done
) &
SNAPSHOT_PID=$!
trap 'kill "$SNAPSHOT_PID" 2>/dev/null || true' EXIT INT TERM

# Wrapper location: $CLAUDE_PTY_WRAPPER, else ~/bin/claude_pty_wrapper.py.
WRAPPER="${CLAUDE_PTY_WRAPPER:-$HOME/bin/claude_pty_wrapper.py}"
if [ ! -x "$WRAPPER" ] && [ ! -f "$WRAPPER" ]; then
  echo "yolo.sh: wrapper not found at $WRAPPER (set CLAUDE_PTY_WRAPPER to override)" >&2
  exit 1
fi

exec python3 "$WRAPPER" --dangerously-skip-permissions --continue "$@"
