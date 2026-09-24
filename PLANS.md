# 本リポジトリの目標

## AIによるループエンジンの構築

このリポジトリはNakano-Official氏のLoop-Engineをフォークしたものであり、以下の目標を達成することを目的としています。

- Claude Codeのみで完結するループエンジンの構築
    - 現状は、プランナーとクリティックがClaude Codeで動作し、ソルバーはCodex CLIやローカルモデルを使用しています。将来的には、ソルバーもClaude Codeで実装可能にすることを目指します。
    - これにより、全ての役割をClaude Codeで完結させることができ、高額な機材や追加のサブスクリプションを必要とせず、より多くの人がループエンジンを利用できるようになります。
    - ただし、Claudeのマルチエージェント化を行う必要があり、それらの実装が必要です。

## 目標達成に向けての具体的な作業

上から順に着手する。

| 作業内容 | 目的 | 操作ファイル |
|---|---|---|
| Claude Code のソルバーバックエンド `solver-claude` を作る | `solver-run` が名前 `claude` で呼べる実体を置く。形は `planner-run` に揃える（使い捨て HOME、`--safe-mode`、`--setting-sources ""`、`--no-session-persistence`、内側の `timeout`）。ツールは `Read Write Edit` だけを許し、Bash は許さない。テストの判定はランナーの VERIFY だけが行う | `provision/bin/solver-claude`（新規） |
| ソルバーの資格情報ファイルを作る | `/etc/loop/solver.env`（`root:solver 0640`）に `CLAUDE_CODE_OAUTH_TOKEN` を置く。未認証の報告を codex ログインから token の有無に替える | `provision/45-agent-invoke.sh` |
| `solver-claude` を配置する | `/srv/loop/bin/solver-claude` を root 所有 0755 で install する。ランナーは名前しか渡せないので、実体の追加はここでしかできない | `provision/45-agent-invoke.sh` |
| 資格情報の柵を検査する | solver が `planner.env` `critic.env` を読めないこと、planner と critic が `solver.env` を読めないことを、各役の視点で assert する | `provision/40-perms.sh`、`provision/45-agent-invoke.sh` |
| `solver-run` の既定バックエンドを `claude` にする | 名前省略時に codex へ落ちない。codex 分岐は任意のバックエンドとして残す | `provision/bin/solver-run` |
| ランナーの既定 tier を `["claude"]` にする | 計画が `solver_tiers` を書かないときに Claude だけで回る | `runner/loop.py`（`SOLVER_TIERS`）、`runner/tests/test_attempts.py` |
| 利用上限への到達を失敗と区別する | 3役が同じサブスクリプションの枠を共有する。上限到達が Halt → エスカレーションに化け、エスカレーションがさらにプランナー呼び出しで枠を使う連鎖を止める。launcher は専用の終了コードを返し、ランナーは待ってから同じ呼び出しをやり直す | `provision/bin/solver-claude`、`planner-run`、`critic-run`、`runner/loop.py`（`call_solver` `call_planner` `call_critic`） |
| 役ごとにモデルを指定する | 各 `*.env` の `LOOP_MODEL` を `--model` で渡す。呼び出し回数が最も多いソルバーに軽いモデルを割り当てられる | `provision/bin/*-run`、`provision/bin/solver-claude`、`provision/45-agent-invoke.sh` |
| 消費量を台帳に残す | `--output-format json` の usage を `ledger` に記録し、どの役が枠を使ったかを run 後に読める | `provision/bin/*-run`、`runner/loop.py` |
| smoke を Claude ソルバーで通す | Runas、認証、非対話実行、書いたファイルの所有者が solver であることを確認する | `provision/bin/smoke-solver`、`provision/bin/smoke-pytest`、`provision/bin/smoke-dom` |
| fixture で実走する | 3ステップの最小計画で、関門が Claude ソルバーの出力を正しく裁くことを確かめる | `plan/fixture/` |
| run 8 の計画を Claude ソルバーで回す | run 5（Codex）、run 6（ローカル 9B）と同じ物差しで比較する | `docs/HANDOFF.md` |
| ドキュメントを Claude 単独構成に揃える | 役の表、資格情報の置き場、未決事項、tier の例を現構成に合わせる | `README.md`、`docs/ARCHITECTURE.md`、`docs/RUNNER_SPEC.md`（§4-4-1、§11-1）、`docs/LOCAL_SOLVER.md`、`provision/README.md`、`provision/70-local-solver.sh`、`.gitignore` |
| 英語コメントの日本語化 | 日本人である私が読めるようにする | 英語コメントアウト全般 |

### 着手前に決めること

- サブスクリプションを1つで回すか、役ごとに分けるか
  - 1つなら、役ごとに資格情報を分けても枠は分かれない
  - 人間の対話作業とも同じ枠を取り合う
- codex とローカルモデルのバックエンドを残すか
  - 残す場合、Claude 単独構成を既定にして任意の tier として扱う

## 更新履歴

- 2026/09/24: ソルバーのツールを Read Write Edit に決定
- 2026/09/23: 具体的な作業の表と着手前に決めることを追加