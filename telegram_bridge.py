#!/usr/bin/env python3
"""Bridge between a Claude Code session and a Telegram bot.

Forward direction (Claude -> Telegram):
  - Tails the active session JSONL
  - Sends every assistant text response to the registered Telegram chat
  - Truncates >4000 chars per message; splits across multiple messages

Reverse direction (Telegram -> Claude):
  - Receives messages from the registered chat
  - Tries to inject them into the live Claude session via the pty wrapper's
    Unix socket at /tmp/claude-inject.sock (so they appear as if you typed)
  - Falls back to appending to ~/Workspace/phone-queue.txt if the wrapper
    isn't running. (A UserPromptSubmit hook can flush the queue on next turn.)

Whitelist: only the FIRST chat_id that messages the bot is registered.
Subsequent messages from other chats are ignored.

Configuration files (auto-created where noted):
  ~/.claude-bridge/telegram-token         bot token from @BotFather (chmod 600)
  ~/.claude-bridge/telegram-chat-id       registered chat id (auto-created)

Environment overrides:
  CLAUDE_BRIDGE_DIR        config dir (default: ~/.claude-bridge)
  CLAUDE_SESSION_DIR       Claude Code project session dir
                           (default: derived from $PWD by Claude Code's naming
                            convention, ~/.claude/projects/-<slug>)
  CLAUDE_INJECT_SOCK       pty-wrapper socket (default: /tmp/claude-inject.sock)
  TELEGRAM_BRIDGE_NOTIFY   set 1 to also write a cosmetic [telegram HH:MM]
                           line to claude's controlling tty when the inject
                           socket is unavailable (default: off)
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from telegram import Bot, Update
    from telegram.ext import Application, MessageHandler, filters, ContextTypes
except ImportError:
    sys.stderr.write(
        "python-telegram-bot not installed. "
        "Run: pip install --user --break-system-packages python-telegram-bot\n"
    )
    sys.exit(1)

HOME = Path.home()
BRIDGE_DIR = Path(os.environ.get("CLAUDE_BRIDGE_DIR", str(HOME / ".claude-bridge")))
TOKEN_FILE = BRIDGE_DIR / "telegram-token"
CHAT_ID_FILE = BRIDGE_DIR / "telegram-chat-id"
QUEUE_FILE = HOME / "Workspace" / "phone-queue.txt"
LOG_FILE = HOME / "Workspace" / "telegram-bridge.log"
INJECT_SOCK = os.environ.get("CLAUDE_INJECT_SOCK", "/tmp/claude-inject.sock")
CHUNK = 4000  # Telegram per-message char limit


def claude_project_dir_for(cwd: Path) -> Path:
    """Mirror Claude Code's project-dir naming: /home/x/Workspace -> -home-x-Workspace."""
    return HOME / ".claude" / "projects" / cwd.as_posix().replace("/", "-")


SESSION_DIR = Path(
    os.environ.get("CLAUDE_SESSION_DIR", str(claude_project_dir_for(Path.cwd())))
)


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_active_session() -> Path | None:
    """Most-recently-modified jsonl file in the project session dir."""
    if not SESSION_DIR.exists():
        return None
    files = list(SESSION_DIR.glob("*.jsonl"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def get_chat_id() -> int | None:
    if CHAT_ID_FILE.exists():
        try:
            return int(CHAT_ID_FILE.read_text().strip())
        except Exception:
            return None
    return None


def set_chat_id(chat_id: int) -> None:
    CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHAT_ID_FILE.write_text(str(chat_id))
    os.chmod(CHAT_ID_FILE, 0o600)


def extract_assistant_text(line: str) -> list[str]:
    try:
        m = json.loads(line)
    except Exception:
        return []
    if m.get("type") != "assistant":
        return []
    out = []
    for c in (m.get("message", {}) or {}).get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text") or ""
            if t.strip():
                out.append(t)
    return out


async def tail_session_to_telegram(bot: Bot) -> None:
    session = find_active_session()
    if not session:
        log("no session jsonl found yet; will retry")
    last_path = session
    last_size = session.stat().st_size if session else 0

    while True:
        try:
            chat_id = get_chat_id()
            session = find_active_session()
            if session is None:
                await asyncio.sleep(2)
                continue
            if last_path != session:
                last_path = session
                last_size = session.stat().st_size
            cur_size = session.stat().st_size
            if cur_size > last_size and chat_id is not None:
                with open(session, "rb") as f:
                    f.seek(last_size)
                    chunk = f.read(cur_size - last_size)
                last_size = cur_size
                for line in chunk.decode(errors="replace").split("\n"):
                    if not line.strip():
                        continue
                    for text in extract_assistant_text(line):
                        for i in range(0, len(text), CHUNK):
                            try:
                                await bot.send_message(
                                    chat_id=chat_id, text=text[i : i + CHUNK]
                                )
                            except Exception as e:
                                log(f"send failed: {e}")
            elif cur_size > last_size:
                last_size = cur_size
        except Exception as e:
            log(f"tail loop error: {e}")
        await asyncio.sleep(1)


def notify_terminal(text: str) -> bool:
    """Cosmetic: write [telegram HH:MM] to claude's controlling tty(s)."""
    import subprocess

    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,tty,comm,args"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception as e:
        log(f"notify_terminal ps error: {e}")
        return False

    ttys: set[str] = set()
    for line in out.splitlines():
        if "claude" not in line:
            continue
        if "telegram_bridge" in line or "grep" in line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        tty = parts[1]
        if tty in ("?", "-"):
            continue
        ttys.add(f"/dev/{tty}")

    if not ttys:
        return False

    stamp = datetime.now().strftime("%H:%M:%S")
    msg = f"\r\n\x1b[36m[telegram {stamp}]\x1b[0m {text}\r\n"
    wrote_any = False
    for path in ttys:
        try:
            with open(path, "w") as f:
                f.write(msg)
                f.flush()
            wrote_any = True
        except Exception as e:
            log(f"notify_terminal write {path} failed: {e}")
    return wrote_any


async def on_telegram_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    registered = get_chat_id()
    if registered is None:
        set_chat_id(chat_id)
        username = update.effective_user.username if update.effective_user else "?"
        log(f"registered chat_id={chat_id} from user={username}")
        await msg.reply_text(
            "Registered. You will now receive Claude responses here. "
            "Messages you send will be auto-typed into the live Claude session "
            "if it was launched via the pty wrapper (yolo.sh)."
        )
        return
    if chat_id != registered:
        log(f"ignoring message from non-registered chat {chat_id}")
        return

    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg.text}\n"
    with open(QUEUE_FILE, "a") as f:
        f.write(line)

    injected = False
    if os.path.exists(INJECT_SOCK):
        try:
            import socket as _sock
            s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(INJECT_SOCK)
            payload = msg.text if msg.text.endswith("\n") else msg.text + "\n"
            s.sendall(payload.encode("utf-8"))
            s.close()
            injected = True
            try:
                QUEUE_FILE.write_text("")
            except Exception:
                pass
        except Exception as e:
            log(f"inject socket error: {e}")

    if not injected and os.environ.get("TELEGRAM_BRIDGE_NOTIFY") == "1":
        try:
            if notify_terminal(msg.text):
                log(f"notified terminal: {msg.text[:60]}")
        except Exception as e:
            log(f"notify_terminal raised: {e}")

    if injected:
        log(f"injected via pty socket: {msg.text[:60]}")
        await msg.reply_text(f"sent ({len(msg.text)} chars)")
    else:
        log(f"queued (no wrapper socket; flush on next submit): {msg.text[:60]}")
        await msg.reply_text(
            f"queued ({len(msg.text)} chars; relaunch claude via `yolo.sh` for live auto-type)"
        )


async def main() -> None:
    if not TOKEN_FILE.exists():
        sys.stderr.write(
            f"missing token at {TOKEN_FILE}; create the bot via @BotFather and "
            f"save the token there (chmod 600)\n"
        )
        sys.exit(1)
    token = TOKEN_FILE.read_text().strip()

    log("starting telegram-bridge")
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_telegram_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log("telegram polling up")

    try:
        await tail_session_to_telegram(app.bot)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
