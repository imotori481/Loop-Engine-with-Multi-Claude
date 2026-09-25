"""人間が設定し、名前を付けた成果物の起動だけを実行する。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class Launchers:
    def __init__(self, config_path: Path):
        self.config_path = config_path

    def public(self) -> list[dict[str, str]]:
        return [
            {"id": key, "label": str(value.get("label", key))}
            for key, value in self._items().items()
        ]

    def launch(self, launcher_id: str) -> int:
        item = self._items().get(launcher_id)
        if item is None:
            raise ValueError("unknown launcher")
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise ValueError("launcher argv must be a non-empty string array")
        cwd = item.get("cwd")
        # shell=False と、信用できるホスト側のファイルから ID で選ぶことの
        # 両方が要になっている。計画、ソルバーの出力、HTTP の値は、どれも
        # コマンドラインにならない。
        process = subprocess.Popen(argv, cwd=cwd or None, shell=False)
        return process.pid

    def _items(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        items = data.get("launchers", {}) if isinstance(data, dict) else {}
        if not isinstance(items, dict):
            raise ValueError("launchers must be an object")
        return {str(key): value for key, value in items.items() if isinstance(value, dict)}
