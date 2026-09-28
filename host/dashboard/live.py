"""箱の「いまの作業」を SSH で引く。

ミラーの台帳が進むのは、ランナーが push する GREEN と plan apply のときだけだ。
いま誰がどのステップの何をしているかは、箱の `loop now` に訊くしかない。

コマンドは固定で、HTTP の値は1つも入らない。`shell=False` で引数配列のまま
実行する。何人がページを開いていても SSH が束にならないよう、数秒だけ控えを返す。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_HOST = "loop-dev"
CACHE_SECONDS = 3.0
TIMEOUT_SECONDS = 15


class Live:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._at = 0.0

    def ssh_host(self) -> str | None:
        """config.json の live.ssh_host。live.enabled が false なら None。"""
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return DEFAULT_HOST
        live = data.get("live", {}) if isinstance(data, dict) else {}
        if not isinstance(live, dict):
            return DEFAULT_HOST
        if live.get("enabled") is False:
            return None
        host = live.get("ssh_host", DEFAULT_HOST)
        # ssh の引数になる値。オプションとして読まれうるものは受け付けない。
        if not isinstance(host, str) or not host or host.startswith("-"):
            raise ValueError("live.ssh_host must be a host name")
        return host

    def fetch(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and time.monotonic() - self._at < CACHE_SECONDS:
                return self._cached
            self._cached, self._at = self._ask(), time.monotonic()
            return self._cached

    def _ask(self) -> dict[str, Any]:
        try:
            host = self.ssh_host()
        except ValueError as error:
            return {"error": str(error)}
        if host is None:
            return {"error": "live view is disabled in config.json"}
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "loop", "now"]
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                  encoding="utf-8", timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"error": f"could not reach the sandbox: {error}"}
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout).strip().splitlines()
            return {"error": "could not reach the sandbox"
                             + (f": {reason[-1]}" if reason else "")}
        try:
            value = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"error": "the sandbox answered with something that is not JSON"}
        return value if isinstance(value, dict) else {"error": "unexpected answer"}
