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

    def _run(self, command: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        """箱で固定のコマンドを流す。届かなければ ValueError。"""
        host = self.ssh_host()
        if host is None:
            raise ValueError("live view is disabled in config.json")
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, *command]
        try:
            return subprocess.run(argv, shell=False, capture_output=True, text=True,
                                  encoding="utf-8", input=stdin, timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"could not reach the sandbox: {error}")

    def _act(self, command: list[str], stdin: str | None = None) -> str:
        """箱を書き換えるコマンドを流す。断られたら、箱の言葉で ValueError。"""
        proc = self._run(command, stdin)
        with self._lock:
            self._cached = None
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        if proc.returncode != 0:
            raise ValueError(lines[-1] if lines else f"the sandbox refused ({proc.returncode})")
        return proc.stdout.strip()

    def rewrite_finding(self, mode: str, index: int, title: str, evidence: str) -> str:
        """クリティックの指摘1件の title と evidence を書き換える。

        値は標準入力の JSON で渡し、コマンドラインには入れない。何を受け付けるかは
        箱の loop_findings.py が決める。
        """
        request = {"mode": mode, "index": index, "title": title, "evidence": evidence}
        return self._act(["loop", "findings", "set"], json.dumps(request, ensure_ascii=False))

    def continue_loop(self) -> str:
        """`loop continue` を流す。sudoers が保守ユーザーにこれだけをパスワード無しで許す。"""
        return self._act(["sudo", "-n", "/usr/local/bin/loop", "continue"])

    def _ask(self) -> dict[str, Any]:
        try:
            proc = self._run(["loop", "now"])
        except ValueError as error:
            return {"error": str(error)}
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout).strip().splitlines()
            return {"error": "could not reach the sandbox"
                             + (f": {reason[-1]}" if reason else "")}
        try:
            value = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"error": "the sandbox answered with something that is not JSON"}
        return value if isinstance(value, dict) else {"error": "unexpected answer"}
