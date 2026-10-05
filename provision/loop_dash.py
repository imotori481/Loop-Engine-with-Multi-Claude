"""`loop dash` の中身。ホストのダッシュボードが SSH で流す要求を確かめ、役の設定を読み書きする。

`loop dash` は sudoers が保守ユーザーにパスワード無しで許す唯一の入口で、root で動く。
要求は標準入力の JSON 1つで、コマンドラインには何も載らない。ここで形と値を確かめ、
確かめた値だけを1行ずつ返す。`loop` はその行で動く。

役の設定は /etc/loop/<役>.env の LOOP_MODEL と LOOP_EFFORT の行だけを読み書きする。
同じファイルにある資格情報は読み出さず、書き換えもしない。起動スクリプトはこのファイルを
シェルで読み込むので、書く値は使える文字を絞ってから書く。

    python3 loop_dash.py request <作業ディレクトリ>   # 標準入力に要求の JSON
    python3 loop_dash.py settings <ディレクトリ>
    python3 loop_dash.py set-model <ディレクトリ> <役> <モデル> <effort>
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
# やり直すステップの ID。ランナーは凍結のマニフェストのファイル名に使うので、/ は通さない。
STEP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_REQUIREMENTS = 200_000
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


def check_step(value: object) -> str:
    if not isinstance(value, str) or not STEP.fullmatch(value):
        raise Refused(f"ステップの ID に使えない: {value!r}")
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
    """要求を確かめ、`loop` に返す行を作る。要件の本文だけは作業ディレクトリに書く。"""
    if not isinstance(request, dict):
        raise Refused("JSON のオブジェクトを渡す")
    action = request.get("action")
    if action in ("settings", "continue", "stop"):
        return [action]
    if action == "model":
        return ["model", check_role(request.get("role")), check_model(request.get("model")),
                check_effort(request.get("effort"))]
    if action == "use":
        return ["use", check_project(request.get("project"))]
    if action == "reset":
        return ["reset", check_step(request.get("step"))]
    if action == "init":
        # ホストの取り込みの3つ目の手順。空の bare を用意し、push を待つ。
        src, tests = check_fence(request.get("src", ""), request.get("tests", ""))
        return ["init", check_project(request.get("project")),
                check_branch(request.get("branch")), src, tests]
    if action == "go":
        language, text = request.get("language"), request.get("requirements")
        if language not in loop.LANGUAGES:
            raise Refused(f"知らない言語: {language!r}")
        if not isinstance(text, str) or not text.strip():
            raise Refused("要件が空")
        if len(text) > MAX_REQUIREMENTS:
            raise Refused(f"要件が長すぎる。{MAX_REQUIREMENTS} 字まで")
        with open(os.path.join(work, "requirements.md"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return ["go", language]
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
    return {"roles": roles, "efforts": list(EFFORTS), "languages": sorted(loop.LANGUAGES)}


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
    print("使い方: loop_dash.py request <dir> | settings <dir> | set-model <dir> <役> <モデル> <effort>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
