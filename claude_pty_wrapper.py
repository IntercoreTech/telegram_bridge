#!/usr/bin/env python3
"""Pty wrapper for Claude Code that exposes a Unix socket for async input
injection. The Telegram bridge (or any other process) connects to the socket
and pushes messages into Claude as if you'd typed them.

Usage:
    claude_pty_wrapper.py [args passed to claude]

How it works:
  - We pty.fork() to spawn `claude` with its stdin/stdout/stderr attached
    to a slave pty.
  - We hold the master fd. Anything we write to the master appears as
    keyboard input to claude.
  - Three I/O sources are multiplexed on the master fd:
      1. The user's real terminal stdin -> master (normal typing)
      2. master -> user's terminal stdout (claude's output)
      3. Unix socket reads (Telegram messages) -> master (async injection)
  - Window-resize signals (SIGWINCH) are forwarded so claude knows the
    terminal size.

Security note: the socket is owned by the user (mode 0600). Only processes
running as the same user can connect. Suitable for a single-user dev box or
Tailscale-protected single-user server.

Env:
  CLAUDE_INJECT_SOCK   Unix socket path (default /tmp/claude-inject.sock)
  CLAUDE_BIN           path to the real claude binary
                       (default /home/$USER/.local/bin/claude-bin, the
                       renamed real binary; falls back to `claude` on PATH)
"""
import os, sys, pty, select, signal, struct, fcntl, termios, tty
import socket, threading, time

SOCK_PATH = os.environ.get('CLAUDE_INJECT_SOCK', '/tmp/claude-inject.sock')
CLAUDE_BIN = os.environ.get(
    'CLAUDE_BIN',
    os.path.expanduser('~/.local/bin/claude-bin')
)


def _set_winsize(fd, rows, cols):
    s = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, s)


def _get_winsize(fd):
    s = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\x00' * 8)
    return struct.unpack('HHHH', s)[:2]


def serve_inject_socket(master_fd, sock_path):
    """Listen on a Unix socket. For each connection, read all bytes and
    write them (followed by Enter if missing) to the pty master fd.
    Survives reconnects."""
    try: os.unlink(sock_path)
    except FileNotFoundError: pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    os.chmod(sock_path, 0o600)
    server.listen(8)

    while True:
        try:
            conn, _ = server.accept()
        except Exception:
            continue
        try:
            data = b''
            while True:
                chunk = conn.recv(4096)
                if not chunk: break
                data += chunk
            if data:
                # Strip any trailing newlines — we'll send a separate Enter.
                data = data.rstrip(b'\r\n')
                # Write the text body to pty master in small slices.
                for i in range(0, len(data), 256):
                    os.write(master_fd, data[i:i+256])
                    time.sleep(0.01)
                # Brief pause so the TUI processes the typed text, then send
                # a carriage return (\r) — that's what a real Enter keystroke
                # generates when the terminal is in raw mode. Claude Code's
                # TUI listens for \r as the submit key, not \n.
                time.sleep(0.15)
                os.write(master_fd, b'\r')
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass


def main(argv):
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: exec claude with the requested args
        try:
            os.execvp(CLAUDE_BIN, [CLAUDE_BIN] + argv)
        except FileNotFoundError:
            try:
                os.execvp('claude', ['claude'] + argv)
            except FileNotFoundError:
                sys.stderr.write(
                    f"claude binary not found at {CLAUDE_BIN} or on PATH\n"
                )
                os._exit(127)

    # Parent: proxy I/O between user's terminal and pty master, plus the inject socket.
    def _on_winch(signum, frame):
        try:
            rows, cols = _get_winsize(sys.stdin.fileno())
            _set_winsize(master_fd, rows, cols)
        except Exception:
            pass
    signal.signal(signal.SIGWINCH, _on_winch)
    try:
        rows, cols = _get_winsize(sys.stdin.fileno())
        _set_winsize(master_fd, rows, cols)
    except Exception: pass

    # Put our terminal in raw mode so user keystrokes pass through verbatim
    # and we can write claude's raw output back without local cooking.
    old_attrs = None
    try:
        if sys.stdin.isatty():
            old_attrs = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
    except Exception:
        old_attrs = None

    # Start socket server in a background thread
    t = threading.Thread(target=serve_inject_socket, args=(master_fd, SOCK_PATH), daemon=True)
    t.start()

    fds = [sys.stdin.fileno(), master_fd]
    try:
        while True:
            try:
                rlist, _, _ = select.select(fds, [], [])
            except InterruptedError:
                continue
            if sys.stdin.fileno() in rlist:
                try: data = os.read(sys.stdin.fileno(), 4096)
                except OSError: data = b''
                if not data: break
                os.write(master_fd, data)
            if master_fd in rlist:
                try: data = os.read(master_fd, 4096)
                except OSError: data = b''
                if not data: break
                os.write(sys.stdout.fileno(), data)
    finally:
        if old_attrs is not None:
            try: termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
            except Exception: pass
        try: os.close(master_fd)
        except Exception: pass
        try: os.unlink(SOCK_PATH)
        except Exception: pass
        try:
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                sys.exit(os.WEXITSTATUS(status))
            else:
                sys.exit(1)
        except Exception:
            sys.exit(0)


if __name__ == '__main__':
    main(sys.argv[1:])
