"""箱の「いまの作業」を SSH で引く。

ミラーの台帳が進むのは、ランナーが push する GREEN と段のコマンドのときだけで、
写しは loop-pull しないと進まない。いま誰がどのモジュールの何をしているかと、せきで
読む設計は、箱に訊く。

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

    def _dash(self, request: dict[str, Any]) -> str:
        """`loop dash` に要求を1つ渡す。sudoers が保守ユーザーにこれだけをパスワード無しで許す。

        要求は標準入力の JSON で渡し、コマンドラインは固定のまま。何を受け付けるかは箱の
        loop_dash.py が決める。
        """
        return self._act(["sudo", "-n", "/usr/local/bin/loop", "dash"],
                         json.dumps(request, ensure_ascii=False))

    def continue_loop(self) -> str:
        return self._dash({"action": "continue"})

    def stop_loop(self) -> str:
        return self._dash({"action": "stop"})

    def reset_module(self, module: str) -> str:
        """`loop raw reset <モジュール>`。止まったモジュールの作業ツリーを最後のコミットに戻す。"""
        return self._dash({"action": "reset", "module": module})

    def approve(self) -> str:
        """`loop approve`。今の段の設計を承認し、次の段へ流す。"""
        return self._dash({"action": "approve"})

    def reject(self, comment: str) -> str:
        """`loop reject`。コメントを付けて、今の段の設計をプランナーに書き直させる。"""
        return self._dash({"action": "reject", "text": comment})

    def reopen(self, modules: list[str]) -> str:
        """`loop reopen`。詳細設計を直すモジュールを選び、中段に戻す。"""
        return self._dash({"action": "reopen", "modules": modules})

    def start_loop(self, spec: str, language: str, framework: str = "") -> str:
        """`loop go`。仕様書の本文は箱の一時ディレクトリに書かれ、そこから置かれる。
        持ち込む先が空なら、箱が言語の既定を選ぶ。"""
        return self._dash({"action": "go", "spec": spec, "language": language,
                           "framework": framework})

    def use_project(self, name: str) -> str:
        """`loop project use` を箱の一時ユニットで裏に回す。進みは `loop now` の switch に出る。"""
        return self._dash({"action": "use", "project": name})

    def remove_project(self, name: str) -> str:
        """`loop project remove`。箱のプロジェクトをディレクトリと走行ログごと消す。"""
        return self._dash({"action": "remove", "project": name})

    def init_project(self, name: str, branch: str, src: str = "", tests: str = "") -> str:
        """`loop project init <名前> --branch <ブランチ>`。取り込むブランチの push を待つ空の bare を作る。"""
        return self._dash({"action": "init", "project": name, "branch": branch,
                           "src": src, "tests": tests})

    def _read(self, action: str) -> dict[str, Any]:
        """読むだけの要求を流し、JSON のオブジェクトを返す。"""
        try:
            value = json.loads(self._dash({"action": action}))
        except json.JSONDecodeError:
            raise ValueError("the sandbox answered with something that is not JSON")
        if not isinstance(value, dict):
            raise ValueError("unexpected answer")
        return value

    def settings(self) -> dict[str, Any]:
        """役ごとの LOOP_MODEL と LOOP_EFFORT、選べる effort と言語。資格情報は返らない。"""
        return self._read("settings")

    def design(self) -> dict[str, Any]:
        """段、基本設計、詳細設計、受け入れ条件、インターフェースチェック、批評、止まった理由。"""
        return self._read("design")

    def set_model(self, role: str, model: str, effort: str) -> str:
        """/etc/loop/<役>.env の LOOP_MODEL と LOOP_EFFORT を書き換える。"""
        return self._dash({"action": "model", "role": role, "model": model, "effort": effort})

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
