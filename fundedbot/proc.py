"""Keeping track of the bot process: a pid file, a stop flag and a 'keep the PC awake' switch.

The bot writes bot.pid next to its state file while it runs and stops by itself when bot.stop appears there,
so the dashboard (or anything else) can see and stop it without being its parent process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill(pid: int):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        os.kill(pid, 15)


def read_pid(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "bot.pid").read_text())
    except (OSError, ValueError):
        return None


def write_pid(folder: Path, mode: str, state_file: Path):
    (folder / "bot.pid").write_text(json.dumps({"pid": os.getpid(), "mode": mode, "state": state_file.name,
                                                "started": int(time.time() * 1000)}))


def clear_pid(folder: Path):
    info = read_pid(folder)
    if info and info.get("pid") == os.getpid():
        (folder / "bot.pid").unlink(missing_ok=True)


def keep_awake(on: bool):
    """Ask Windows not to go to sleep while the bot runs (no effect elsewhere)."""
    if sys.platform != "win32":
        return
    import ctypes
    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))
