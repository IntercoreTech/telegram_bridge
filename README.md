# Claude Code <-> Telegram bridge

Drive a [Claude Code](https://claude.com/claude-code) session from your phone
over Telegram. Bidirectional:

- **Claude -> Telegram**: every assistant text response is forwarded to your
  registered Telegram chat. Long messages are split across the 4000-char
  per-message limit.
- **Telegram -> Claude**: messages you send to the bot are auto-typed into
  the live `claude` session via a Unix socket the pty wrapper owns. They
  appear as if you typed them in the real terminal.

If the pty wrapper isn't running (e.g. Claude was launched plain), incoming
messages fall back to a queue file (`~/Workspace/phone-queue.txt`).

## Components

| File | Purpose |
| --- | --- |
| `telegram_bridge.py` | Long-running daemon: tails the active Claude session JSONL, polls Telegram, injects messages into the pty wrapper. |
| `claude_pty_wrapper.py` | pty.fork()s `claude` and exposes `/tmp/claude-inject.sock`. Anything written to the socket becomes keystrokes in the live TUI. |
| `yolo.sh` | Convenience launcher: `cd ~/Workspace`, snapshots the session JSONL every 20 min, exec's the pty wrapper with `--dangerously-skip-permissions --continue`. |
| `telegram-bridge.service` | systemd `--user` unit so the bridge auto-starts and restarts on failure. |
| `install.sh` | One-shot setup on a new machine. |

## Quick install (new machine)

```bash
git clone https://github.com/IntercoreTech/telegram_bridge.git
cd telegram_bridge
./install.sh
```

The installer:

1. Installs `python-telegram-bot` (user pip).
2. Drops `claude_pty_wrapper.py` and `yolo.sh` into `~/bin/`.
3. Prompts for your Telegram bot token (hidden input) and writes it to
   `~/.claude-bridge/telegram-token` (mode 600).
4. Installs the systemd `--user` unit, enables `loginctl --linger` so it
   survives logout, starts the service.

## Set up the bot (one time)

1. Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`,
   pick a name + username, copy the API token it returns.
2. Paste that token into the `install.sh` prompt (or write it manually to
   `~/.claude-bridge/telegram-token`).
3. Open the new bot in Telegram and send it any message. The bridge
   registers the **first** chat that messages it as the whitelist; messages
   from any other chat are ignored.

## Daily use

```bash
yolo.sh         # launches Claude Code via the pty wrapper
```

Now any message you send from Telegram lands in the running session as if
typed. Assistant responses appear back in Telegram.

## Configuration overrides

Environment variables read by the daemon:

| Var | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_BRIDGE_DIR` | `~/.claude-bridge` | Token + chat-id directory. |
| `CLAUDE_INJECT_SOCK` | `/tmp/claude-inject.sock` | Unix socket the wrapper owns. |
| `CLAUDE_SESSION_DIR` | derived from `$PWD` | Directory Claude Code stores session JSONLs in. Default mirrors Claude Code's `~/.claude/projects/-<slug>` naming. |
| `TELEGRAM_BRIDGE_NOTIFY` | unset | Set to `1` to also write a cosmetic `[telegram HH:MM]` line to claude's tty when the inject socket is unavailable. |

Read by the pty wrapper:

| Var | Default |
| --- | --- |
| `CLAUDE_INJECT_SOCK` | `/tmp/claude-inject.sock` |
| `CLAUDE_BIN` | `~/.local/bin/claude-bin`, falls back to `claude` on PATH |

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Bridge log says `inject socket error: Connection refused` | Claude was started without the pty wrapper. | Relaunch with `yolo.sh`. The message is queued meanwhile. |
| `journalctl --user -u telegram-bridge -n 50` shows `httpx.ReadError` | Transient network blip from Telegram. | The daemon retries automatically; nothing to do. |
| `systemctl --user is-active telegram-bridge` returns `inactive` after logout | `linger` is off. | `sudo loginctl enable-linger $USER` (or run install.sh again). |
| Phone messages arrive but nothing types in the TUI | Wrapper not the one that owns the socket. | `pgrep -af claude_pty_wrapper` — kill stragglers, relaunch via `yolo.sh`. |

## Security model

- The bot whitelists the **first** chat that messages it. Other chats are
  silently ignored. Delete `~/.claude-bridge/telegram-chat-id` to re-pair.
- The inject Unix socket is mode 0600, owned by the user. Only same-user
  processes can connect. Suitable for a single-user host or a host fronted
  by Tailscale / SSH-only access.
- The token file is mode 0600. Don't commit it. The repo's `.gitignore`
  excludes the whole `.claude-bridge/` dir.

## License

MIT — see `LICENSE`.
