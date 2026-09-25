"""誰が訊いているか。それによって何をしてよいかが決まる。

入り口は2つあり、同じではない。この機械の前では、ブラウザとゲームが同じ
デスクトップにある。見ている人は成果物を起動し、動くところを見られる。
tailnet 越しなら相手はスマホだ。ランナーが出した事実はすべて読めるし、作業を
差し戻すこともできる。しかしゲームが開く窓は見えない。だから、ゲームが良いと
言えてはならない。

どちらかは Host ヘッダで決める。ループバックの検査がすでに頼っているのと同じ
値を、同じ使い方で使う。DNS を付け替えてこのポートに届いたページも、自分の
名前を運んだままで、その名前はどちらも一覧に無い。
"""

from __future__ import annotations

import json
from pathlib import Path

LOCAL = "local"
REMOTE = "remote"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def hostname(header: str) -> str:
    """Host ヘッダからホスト名を取り出す。ポートと IPv6 の括弧を外し、小文字にする。"""
    return header.rsplit(":", 1)[0].strip("[]").lower() if header else ""


class Reach:
    """この機械の設定を毎回読む。ダッシュボードの公開と非公開を切り替えても、
    再起動は要らない。"""

    def __init__(self, config_path: Path):
        self.config_path = config_path

    def published(self) -> dict:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        remote = data.get("remote") if isinstance(data, dict) else None
        return remote if isinstance(remote, dict) else {}

    def of(self, headers) -> tuple[str | None, str]:
        """(scope, user) を返す。scope が None なら、その要求は断る。"""
        host = hostname(headers.get("Host", ""))
        if host in LOOPBACK:
            return LOCAL, ""

        remote = self.published()
        expected = hostname(str(remote.get("host", "")))
        allowed = remote.get("users")
        # どちらの条件も、欠けていれば閉じる側に倒す。`remote` が無い、または
        # 人の一覧が無いときは、ダッシュボードは公開されていない。全員に
        # 公開されているのではない。
        if not expected or host != expected:
            return None, ""
        if not isinstance(allowed, list) or not allowed:
            return None, ""

        # このヘッダは tailscale serve のプロキシが書き、クライアントが送った
        # 値を上書きする。信用できるのは、先に Host が一致しているからに
        # すぎない。このヘッダは、届いた名前と同じだけしか信用しない。
        user = headers.get("Tailscale-User-Login", "")
        return (REMOTE, user) if user in allowed else (None, "")
