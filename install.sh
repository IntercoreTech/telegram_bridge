#!/usr/bin/env bash
# install.sh — set up the Claude Code <-> Telegram bridge on a fresh machine.
#
# What it does:
#   1. Installs python-telegram-bot (user pip) if missing.
#   2. Installs claude_pty_wrapper.py to ~/bin/.
#   3. Installs yolo.sh to ~/bin/ (chmod +x).
#   4. Prompts for the Telegram bot token and writes it to
#      ~/.claude-bridge/telegram-token (chmod 600). Skips if already set.
#   5. Installs the systemd --user unit, enables linger, starts the service.
#
# Idempotent — safe to re-run.
#
# Usage:  ./install.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME/bin" "$HOME/Workspace" "$HOME/.claude-bridge" "$HOME/.config/systemd/user"

# 1. Python deps
if ! python3 -c 'import telegram' 2>/dev/null; then
  echo "[install] installing python-telegram-bot..."
  python3 -m pip install --user --break-system-packages python-telegram-bot >/dev/null
fi

# 2. pty wrapper
install -m 0755 "$REPO_DIR/claude_pty_wrapper.py" "$HOME/bin/claude_pty_wrapper.py"
echo "[install] wrote $HOME/bin/claude_pty_wrapper.py"

# 3. yolo.sh
install -m 0755 "$REPO_DIR/yolo.sh" "$HOME/bin/yolo.sh"
echo "[install] wrote $HOME/bin/yolo.sh"
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo "[install] note: add \"$HOME/bin\" to PATH to run 'yolo.sh' bare" ;;
esac

# 4. Token
TOKEN_FILE="$HOME/.claude-bridge/telegram-token"
if [ ! -s "$TOKEN_FILE" ]; then
  echo
  echo "[install] paste the Telegram bot token from @BotFather (input hidden):"
  read -r -s TOKEN
  echo
  if [ -z "$TOKEN" ]; then
    echo "[install] empty token — skipping (you can write it later to $TOKEN_FILE)"
  else
    umask 077
    printf '%s' "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "[install] wrote token to $TOKEN_FILE"
  fi
else
  echo "[install] token already present at $TOKEN_FILE — skipping prompt"
fi

# 5. systemd --user unit
UNIT_SRC="$REPO_DIR/telegram-bridge.service"
UNIT_DST="$HOME/.config/systemd/user/telegram-bridge.service"
# Rewrite the ExecStart path to point at the actual installed bridge script.
sed "s|%h/Workspace/claude-code-telegram-bridge/telegram_bridge.py|$REPO_DIR/telegram_bridge.py|" \
  "$UNIT_SRC" > "$UNIT_DST"
echo "[install] wrote $UNIT_DST"

# Enable linger so user services survive logout / SSH disconnect
if command -v loginctl >/dev/null 2>&1; then
  if [ "$(loginctl show-user "$USER" 2>/dev/null | awk -F= '/^Linger=/{print $2}')" != "yes" ]; then
    if loginctl enable-linger "$USER" 2>/dev/null; then
      echo "[install] enabled linger for $USER"
    else
      echo "[install] could not enable linger — try: sudo loginctl enable-linger $USER"
    fi
  fi
fi

systemctl --user daemon-reload
systemctl --user enable --now telegram-bridge.service
echo
systemctl --user --no-pager status telegram-bridge.service | head -10

cat <<EOF

---
Done.

Next steps:
  1. Send any message to your bot from Telegram. The first chat to message
     becomes the registered chat (whitelist of one).
  2. Launch Claude Code via:  yolo.sh   (or: $HOME/bin/yolo.sh)
     This brings up the pty wrapper that owns /tmp/claude-inject.sock so
     phone messages auto-type into the live session.
  3. To check the bridge: tail -f $HOME/Workspace/telegram-bridge.log
EOF
