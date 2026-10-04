"""ホストの写しを `loop-pull.cmd` で箱の先まで進める。

クローンが初めてなら数分かかるので、要求の中では待たずに裏のスレッドで流し、
進みは status で返す。コマンドは固定で、HTTP の値は1つも入らない。
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "loop-pull.cmd"
TAIL_LINES = 40


class MirrorSync:
    def __init__(self, script: Path = SCRIPT):
        self.script = script
        self._lock = threading.Lock()
        self._running = False
        self._started: str | None = None
        self._finished: str | None = None
        self._returncode: int | None = None
        self._output: deque[str] = deque(maxlen=TAIL_LINES)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._running, "started": self._started,
                    "finished": self._finished, "returncode": self._returncode,
                    "output": list(self._output)}

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                raise ValueError("loop-pull is already running")
            self._running = True
            self._started, self._finished, self._returncode = _now(), None, None
            self._output.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self.status()

    def _run(self) -> None:
        returncode = -1
        try:
            process = subprocess.Popen(
                ["cmd.exe", "/d", "/c", str(self.script)], shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace")
            for line in process.stdout:
                with self._lock:
                    self._output.append(line.rstrip())
            returncode = process.wait()
        except OSError as error:
            with self._lock:
                self._output.append(f"could not start loop-pull: {error}")
        finally:
            with self._lock:
                self._running, self._finished, self._returncode = False, _now(), returncode


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
