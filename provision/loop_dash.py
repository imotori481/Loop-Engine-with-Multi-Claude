"""`loop dash` の中身。ホストのダッシュボードが SSH で流す要求を確かめ、役の設定を読み書きする。

`loop dash` は sudoers が保守ユーザーにパスワード無しで許す唯一の入口で、root で動く。
要求は標準入力の JSON 1つで、コマンドラインには何も載らない。ここで形と値を確かめ、
確かめた値だけを1行ずつ返す。`loop` はその行で動く。

役の設定は /etc/loop/<役>.env の LOOP_MODEL と LOOP_EFFORT の行だけを読み書きする。
同じファイルにある資格情報は読み出さず、書き換えもしない。起動スクリプトはこのファイルを
シェルで読み込むので、書く値は使える文字を絞ってから書く。

設計（段、基本設計、詳細設計、受け入れ条件、インターフェースチェック、批評、止まった
理由）は読むだけで返す。plan/、docs/、ac/ は runner だけが読めるので、root のここで読む。

    python3 loop_dash.py request <作業ディレクトリ>   # 標準入力に要求の JSON
    python3 loop_dash.py settings <ディレクトリ>
    python3 loop_dash.py set-model <ディレクトリ> <役> <モデル> <effort>
    python3 loop_dash.py design <作業ツリー>
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runner"))

import loop  # noqa: E402

ROLES = ("planner", "critic", "solver")
# 起動スクリプト（planner-run、critic-run、solver-claude）が受け付けるものと合わせる。
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# --model に渡す名前。シェルで読み込まれるので、空白、引用符、$ などは通さない。
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,99}")
# loop-project.sh の valid_name と同じ規則。
PROJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# 取り込むブランチ。行にもオプションにもならない文字だけを通す。git の規則
# （check-ref-format）は loop-project.sh の init が確かめる。
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
# 仕様書の本文。
MAX_SPEC = 200_000
# 差し戻しのコメント。ダッシュボードの欄は 4000 字までだが、箱の側でも絞る。
MAX_COMMENT = 20_000
KEYS = {"model": "LOOP_MODEL", "effort": "LOOP_EFFORT"}


class Refused(Exception):
    pass


def check_model(value: object) -> str:
    if not isinstance(value, str) or (value and not MODEL.fullmatch(value)):
        raise Refused(f"モデルの名前に使えない: {value!r}")
    return value


def check_effort(value: object) -> str:
    if value not in ("", *EFFORTS):
        raise Refused(f"effort は {' / '.join(EFFORTS)} のどれかか空: {value!r}")
    return value


def check_role(value: object) -> str:
    if value not in ROLES:
        raise Refused(f"知らない役: {value!r}")
    return value


def check_project(value: object) -> str:
    if not isinstance(value, str) or not PROJECT.fullmatch(value) or value == "CURRENT":
        raise Refused(f"プロジェクト名に使えない: {value!r}")
    return value


def check_branch(value: object) -> str:
    if not isinstance(value, str) or not BRANCH.fullmatch(value):
        raise Refused(f"ブランチ名に使えない: {value!r}")
    return value


def check_module(value: object) -> str:
    """モジュールの id。src/ の下のパスから拡張子を落としたもの（loop.MODULE_ID）。"""
    if not isinstance(value, str) or len(value) > 200 or not loop.MODULE_ID.fullmatch(value):
        raise Refused(f"モジュールの id に使えない: {value!r}")
    return value


def check_fence(src: object, tests: object) -> tuple[str, str]:
    """柵の場所。両方とも空なら既定の src と tests。片方だけなら、もう片方は既定。"""
    if not isinstance(src, str) or not isinstance(tests, str):
        raise Refused("柵の場所は文字列で渡す")
    if src or tests:
        problems = loop.layout_problems({"src": src or "src", "tests": tests or "tests"})
        if problems:
            raise Refused("柵の場所に使えない: " + "; ".join(problems))
    return src, tests


def parse(request: object, work: str) -> list[str]:
    """要求を確かめ、`loop` に返す行を作る。仕様書とコメントの本文は作業ディレクトリに書く。"""
    if not isinstance(request, dict):
        raise Refused("JSON のオブジェクトを渡す")
    action = request.get("action")
    if action in ("settings", "design", "continue", "stop", "approve"):
        return [action]
    if action == "model":
        return ["model", check_role(request.get("role")), check_model(request.get("model")),
                check_effort(request.get("effort"))]
    if action in ("use", "remove"):
        return [action, check_project(request.get("project"))]
    if action == "reset":
        return ["reset", check_module(request.get("module"))]
    if action == "reopen":
        modules = request.get("modules")
        if not isinstance(modules, list) or not modules:
            raise Refused("中段に戻すモジュールを1つ以上選ぶ")
        return ["reopen", *(check_module(m) for m in modules)]
    if action == "init":
        # ホストの取り込みの3つ目の手順。空の bare を用意し、push を待つ。
        src, tests = check_fence(request.get("src", ""), request.get("tests", ""))
        return ["init", check_project(request.get("project")),
                check_branch(request.get("branch")), src, tests]
    if action == "go":
        language, text = request.get("language"), request.get("spec")
        framework = request.get("framework", "")
        if language not in loop.LANGUAGES:
            raise Refused(f"知らない言語: {language!r}")
        # 空なら言語の既定。返す行には、いつも名前を入れる。
        if framework == "":
            framework = loop.LANGUAGES[language]["framework"]
        if framework not in loop.FRAMEWORKS[language]:
            raise Refused(f"{language} の持ち込む先に無い: {framework!r}")
        if not isinstance(text, str) or not text.strip():
            raise Refused("仕様書が空")
        if len(text) > MAX_SPEC:
            raise Refused(f"仕様書が長すぎる。{MAX_SPEC} 字まで")
        with open(os.path.join(work, "spec.md"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return ["go", language, framework]
    if action == "reject":
        # コメントの本文も仕様書と同じく作業ディレクトリに書き、行には載せない。
        text = request.get("text")
        if not isinstance(text, str) or not text.strip():
            raise Refused("コメントが空")
        if len(text) > MAX_COMMENT:
            raise Refused(f"コメントが長すぎる。{MAX_COMMENT} 字まで")
        with open(os.path.join(work, "comment.md"), "w", encoding="utf-8") as handle:
            handle.write(text.strip() + "\n")
        return ["reject"]
    raise Refused(f"知らない操作: {action!r}")


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_role(path: str) -> dict[str, str]:
    """1つの役の LOOP_MODEL と LOOP_EFFORT。ほかの行は返さない。"""
    found = dict.fromkeys(KEYS, "")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            for field, key in KEYS.items():
                if line.startswith(key + "="):
                    found[field] = unquote(line[len(key) + 1:])
    return found


def settings(directory: str) -> dict:
    roles = {}
    for role in ROLES:
        try:
            roles[role] = read_role(os.path.join(directory, f"{role}.env"))
        except OSError:
            roles[role] = None
    # 持ち込む先は言語ごとの一覧で、先頭が言語の既定。
    frameworks = {language: [loop.LANGUAGES[language]["framework"]]
                  + sorted(set(loop.FRAMEWORKS[language]) - {loop.LANGUAGES[language]["framework"]})
                  for language in loop.LANGUAGES}
    return {"roles": roles, "efforts": list(EFFORTS), "languages": sorted(loop.LANGUAGES),
            "frameworks": frameworks}


def read_tree(root: str, suffix: str) -> dict[str, str]:
    """root の下の suffix のファイルを、root からのパスから suffix を落とした名前で読む。"""
    found = {}
    for directory, _, names in os.walk(root):
        for name in sorted(names):
            if not name.endswith(suffix):
                continue
            path = os.path.join(directory, name)
            key = os.path.relpath(path, root)[:-len(suffix)].replace(os.sep, "/")
            with open(path, encoding="utf-8", errors="replace") as handle:
                found[key] = handle.read()
    return dict(sorted(found.items()))


def read_json_file(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def design(project: str) -> dict:
    """人がせきで読むもの。受け入れ条件は読めない JSON なら本文のまま返す。"""
    plan = os.path.join(project, "plan")
    acs = {}
    for key, text in read_tree(os.path.join(project, "ac"), ".json").items():
        try:
            acs[key] = json.loads(text)
        except ValueError:
            acs[key] = text
    try:
        with open(os.path.join(plan, "FAILURE.md"), encoding="utf-8", errors="replace") as handle:
            failure = handle.read()
    except OSError:
        failure = None
    return {"stage": read_json_file(os.path.join(plan, "stage.json")) or {},
            "basic": read_tree(os.path.join(project, "docs", "basic"), ".md"),
            "modules": read_tree(os.path.join(project, "docs", "modules"), ".md"),
            "ac": acs,
            "interface": read_json_file(os.path.join(plan, "INTERFACE.json")),
            "critique": read_json_file(os.path.join(plan, "CRITIQUE.json")),
            "failure": failure}


def set_model(directory: str, role: str, model: str, effort: str) -> None:
    """LOOP_MODEL と LOOP_EFFORT の行を書き換える。行が無ければ足す。

    ほかの行はそのまま残す。同じディレクトリに書いてから rename するので、起動スクリプトが
    書きかけのファイルを読み込むことは無い。所有者と権限は元のファイルのものを引き継ぐ。
    """
    check_role(role)
    values = {"LOOP_MODEL": check_model(model), "LOOP_EFFORT": check_effort(effort)}
    path = os.path.join(directory, f"{role}.env")
    status = os.stat(path)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for key, value in values.items():
        at = [n for n, line in enumerate(lines) if line.startswith(key + "=")]
        if at:
            lines[at[0]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
    fd, temp = tempfile.mkstemp(prefix=f".{role}.env.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if hasattr(os, "chown"):
            os.chown(temp, status.st_uid, status.st_gid)
        os.chmod(temp, status.st_mode & 0o777)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "request":
            try:
                request = json.loads(sys.stdin.read())
            except ValueError as error:
                raise Refused(f"JSON として読めない: {error}")
            print("\n".join(parse(request, argv[2])))
            return 0
        if len(argv) == 3 and argv[1] == "settings":
            print(json.dumps(settings(argv[2]), ensure_ascii=False))
            return 0
        if len(argv) == 3 and argv[1] == "design":
            print(json.dumps(design(argv[2]), ensure_ascii=False))
            return 0
        if len(argv) == 6 and argv[1] == "set-model":
            set_model(*argv[2:6])
            print(f"{argv[3]} の設定を書き換えた。次の呼び出しから効く")
            return 0
    except Refused as error:
        print(f"loop dash: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"loop dash: {error}", file=sys.stderr)
        return 1
    print("使い方: loop_dash.py request <dir> | settings <dir> | design <dir>"
          " | set-model <dir> <役> <モデル> <effort>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
