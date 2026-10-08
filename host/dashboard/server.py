"""Loop Engine の運用者向けダッシュボードの HTTP サーバ。

listen するのは常にループバックだけ。スマホから届かせるには、前に
`tailscale serve` を置く。これが tailnet から 127.0.0.1 へ中継する。だから
ここにあるものは LAN に一切出ず、機械の入り口は1つのまま保たれる。
"""

from __future__ import annotations

import argparse
import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from .access import LOCAL, Reach
    from .actions import Launchers
    from .importer import Importer, ImportJob
    from .live import Live
    from .mirror import MirrorSync
    from .pullrequest import PullRequestError, PullRequests
    from .state import DashboardState
except ImportError:  # 直接実行したとき: python host/dashboard/server.py
    from access import LOCAL, Reach
    from actions import Launchers
    from importer import Importer, ImportJob
    from live import Live
    from mirror import MirrorSync
    from pullrequest import PullRequestError, PullRequests
    from state import DashboardState


STATIC = Path(__file__).with_name("static")
# 要求の本文の上限。要件の本文を運ぶ /api/start がいちばん大きい。
MAX_BODY = 1 << 20


def send_pull_request(state: DashboardState, pulls: PullRequests | None) -> dict | None:
    """写しの最後の ALL_GREEN への承認を、親ブランチへの PR にする。結果を記録して返す。

    失敗しても承認は取り消さない。承認は人が遊んで決めたことで、PR が出せたかどうかは
    ホストの git と gh の事情だ。失敗は記録に残り、画面から出し直せる。
    """
    approval = state.approved()
    if approval is None:
        raise ValueError("there is no approved review to send")
    if pulls is None:
        return None
    try:
        outcome = pulls.open(approval["steps"], approval.get("note", ""))
    except PullRequestError as error:
        outcome = {"error": str(error)}
    return state.record_pull_request(approval["request_id"], outcome)


def handler_for(state: DashboardState, launchers: Launchers, reach: Reach, token: str,
                live: Live | None = None, pulls: PullRequests | None = None,
                mirror: MirrorSync | None = None, imports: ImportJob | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            print("dashboard: " + format % args)

        def _json(self, value, status=HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY:
                raise ValueError("request is too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def _caller(self):
            """(scope, user) を返す。断る要求なら (None, "") を返す。

            127.0.0.1 に bind し、独自のヘッダを必須にすれば、普通の Web
            ページは入れない。そのヘッダは CORS のプリフライトを起こし、
            それに答える do_OPTIONS は無いからだ。DNS リバインディングは
            その両方を破る。攻撃者の名前が 127.0.0.1 に解決されるように
            仕組まれ、そのページとこのサーバが同一オリジンになり、ブラウザは
            止めなくなる。付け替えた要求でも変えられないのが Host ヘッダで、
            そこには攻撃者の名前が残る。だから、このサーバが応じる名前は
            一覧で決め、それ以外はすべて 403 にする。
            """
            return reach.of(self.headers)

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Loop-Token", ""), token)

        def do_GET(self) -> None:
            scope, user = self._caller()
            if scope is None:
                self._json({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path == "/api/session":
                # 何をしてよいかは、断られて知るのではなく先にページへ伝える。
                # 押しても動かないボタンは、最初から置くべきでない。
                self._json({"token": token, "scope": scope, "user": user,
                            "launchers": launchers.public() if scope == LOCAL else []})
                return
            if path == "/api/state":
                self._json(state.snapshot())
                return
            if path == "/api/live":
                self._json(live.fetch() if live is not None
                           else {"error": "live view is not configured"})
                return
            if path == "/api/settings":
                try:
                    if live is None:
                        raise ValueError("live view is not configured")
                    self._json(live.settings())
                except ValueError as error:
                    self._json({"error": str(error)})
                return
            if path == "/api/pull":
                self._json(mirror.status() if mirror is not None
                           else {"error": "loop-pull is not configured"})
                return
            if path == "/api/import":
                self._json(imports.status() if imports is not None
                           else {"error": "import is not configured"})
                return
            files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            name = files.get(path)
            if name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = (STATIC / name).read_bytes()
            kind = "text/html" if name.endswith(".html") else (
                "text/css" if name.endswith(".css") else "text/javascript")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            scope, user = self._caller()
            if scope is None:
                self._json({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
                return
            if not self._authorized():
                self._json({"error": "invalid session token"}, HTTPStatus.FORBIDDEN)
                return
            try:
                body = self._body()
                path = urlparse(self.path).path
                if path == "/api/decision":
                    result = state.decide(
                        str(body.get("kind", "")), str(body.get("request_id", "")),
                        str(body.get("decision", "")), str(body.get("note", "")),
                        scope=scope, user=user,
                    )
                    # 承認はこの機械の前でしかできない（decide が断る）ので、ここに
                    # 来た承認はローカルのものだけだ。
                    if result["kind"] == "review" and result["decision"] == "approve":
                        result = {**result, "pull_request": send_pull_request(state, pulls)}
                    self._json(result, HTTPStatus.CREATED)
                    return
                if path == "/api/pull-request":
                    # GitHub に書き込む。承認と同じく、この機械の前でだけ。
                    if scope != LOCAL:
                        raise ValueError(
                            "a pull request can only be sent from the machine itself")
                    self._json(send_pull_request(state, pulls), HTTPStatus.CREATED)
                    return
                if path == "/api/findings":
                    # クリティックの指摘の書き換え。人がプランナーに渡す言葉を決める
                    # ことで、エスカレーションへの回答と同じく、どこからでもよい。
                    if live is None:
                        raise ValueError("live view is not configured")
                    index = body.get("index")
                    if not isinstance(index, int) or isinstance(index, bool):
                        raise ValueError("index must be an integer")
                    message = live.rewrite_finding(
                        str(body.get("mode", "")), index,
                        str(body.get("title", "")), str(body.get("evidence", "")))
                    self._json({"message": message}, HTTPStatus.OK)
                    return
                if path in ("/api/continue", "/api/stop"):
                    # 続行と停止はどこからでもよい。止まった走行を人が読んだ指摘で続けるのも、
                    # 走行を止めるのも、スマホから届いてほしい操作だ。
                    if live is None:
                        raise ValueError("live view is not configured")
                    act = live.continue_loop if path == "/api/continue" else live.stop_loop
                    self._json({"message": act()}, HTTPStatus.ACCEPTED)
                    return
                if path == "/api/reset":
                    # 止まったステップのやり直しも、どこからでもよい。続行の前の一手で、捨てるのは
                    # そのステップのコミットしていない作業だけだ。
                    if live is None:
                        raise ValueError("live view is not configured")
                    self._json({"message": live.reset_step(str(body.get("step", "")))},
                               HTTPStatus.OK)
                    return
                if path in ("/api/start", "/api/project", "/api/model"):
                    # 走行の開始、プロジェクトの切り替え、役のモデルの変更は、利用枠と箱の
                    # 状態を大きく動かす。この機械の前でだけ。
                    if scope != LOCAL:
                        raise ValueError("this can only be done at the machine itself")
                    if live is None:
                        raise ValueError("live view is not configured")
                    if path == "/api/start":
                        message = live.start_loop(str(body.get("requirements", "")),
                                                  str(body.get("language", "")),
                                                  str(body.get("framework", "")))
                    elif path == "/api/project":
                        message = live.use_project(str(body.get("project", "")))
                    else:
                        message = live.set_model(str(body.get("role", "")),
                                                 str(body.get("model", "")),
                                                 str(body.get("effort", "")))
                    self._json({"message": message}, HTTPStatus.ACCEPTED)
                    return
                if path == "/api/pull":
                    # ホストのディスクに書く。成果物の起動と同じく、この機械の前でだけ。
                    if scope != LOCAL:
                        raise ValueError("loop-pull can only be started at the machine itself")
                    if mirror is None:
                        raise ValueError("loop-pull is not configured")
                    self._json(mirror.start(), HTTPStatus.ACCEPTED)
                    return
                if path == "/api/import":
                    # ホストにクローンし、箱にプロジェクトを作って切り替える。開始と同じく、
                    # この機械の前でだけ。
                    if scope != LOCAL:
                        raise ValueError("an import can only be started at the machine itself")
                    if imports is None:
                        raise ValueError("import is not configured")
                    self._json(imports.start(body), HTTPStatus.ACCEPTED)
                    return
                if path == "/api/launch":
                    # プログラムの起動だけは、画面のある場所でしか意味が無い。
                    # スマホから起動すれば、誰も見ていない窓が開くだけだ。
                    if scope != LOCAL:
                        raise ValueError(
                            "artifacts can only be started at the machine itself")
                    pid = launchers.launch(str(body.get("launcher_id", "")))
                    self._json({"pid": pid}, HTTPStatus.ACCEPTED)
                    return
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Loop Engine operator dashboard")
    parser.add_argument("--project", type=Path, required=True,
                        help="host-side project mirror containing plan/ledger.jsonl")
    parser.add_argument("--mirrors", type=Path, default=None,
                        help="directory holding every mirror for the token history "
                             "(default: the parent of --project)")
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name(".state"))
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    state = DashboardState(args.project, args.data, args.mirrors)
    live = Live(args.config)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        handler_for(state, Launchers(args.config), Reach(args.config), token, live,
                    PullRequests(state.project, state.mirrors / "projects"), MirrorSync(),
                    ImportJob(Importer(live))),
    )
    print(f"Loop dashboard: http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
