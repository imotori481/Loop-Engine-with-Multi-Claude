# 本リポジトリの目標

## AIによるループエンジンの構築

このリポジトリはNakano-Official氏のLoop-Engineをフォークしたものであり、以下の目標を達成することを目的としています。

- Claude Codeのみで完結するループエンジンの構築
    - 現状は、プランナーとクリティックがClaude Codeで動作し、ソルバーはCodex CLIやローカルモデルを使用しています。将来的には、ソルバーもClaude Codeで実装可能にすることを目指します。
    - これにより、全ての役割をClaude Codeで完結させることができ、高額な機材や追加のサブスクリプションを必要とせず、より多くの人がループエンジンを利用できるようになります。
    - ただし、Claudeのマルチエージェント化を行う必要があり、それらの実装が必要です。

## 目標達成に向けての具体的な作業

未着手の行は上から順に着手する。

| 状態 | 作業内容 | 目的 | 実装方法 | 操作ファイル |
|---|---|---|---|---|
| 完了 | Claude Code のソルバーバックエンド `solver-claude` を作る | `solver-run` が名前 `claude` で呼べる実体を置く。テストの判定はランナーの VERIFY だけが行う | bash。`claude -p` を `planner-run` と同じ形で起動する（使い捨て HOME、`--safe-mode`、`--setting-sources ""`、`--no-session-persistence`、`timeout`）。`--allowedTools "Read Write Edit"`、`--disallowedTools "Bash"` | `provision/bin/solver-claude` |
| 完了 | ソルバーの資格情報ファイルを作る | ソルバーの資格情報を、solver だけが読める場所に置く | `claude setup-token` で発行したトークンを `CLAUDE_CODE_OAUTH_TOKEN` に書く。ファイルはヒアドキュメントで作り、`chown root:solver`、`chmod 640`。未認証の判定は `grep` でトークン行の有無を見る | `provision/45-agent-invoke.sh` |
| 完了 | `solver-claude` を配置する | ランナーは名前しか渡せないので、実体の追加はここでしかできない | `install -o root -g root -m 755` で `/srv/loop/bin/` に置く | `provision/45-agent-invoke.sh` |
| 完了 | 資格情報の柵を検査する | 3役の `.env` が互いに読めないことを確かめる | `sudo -u <役> test -r` を役 × ファイルの全組み合わせで回す。`.env` を作るのが 45 なので、検査も 45 で行う | `provision/45-agent-invoke.sh` |
| 完了 | `solver-run` の既定バックエンドを `claude` にする | 名前省略時に codex へ落ちない | 既定値 `${3:-codex}` を `${3:-claude}` に変更。codex 分岐は任意のバックエンドとして残す | `provision/bin/solver-run` |
| 完了 | ランナーの既定 tier を `["claude"]` にする | 計画が `solver_tiers` を書かないときに Claude だけで回る | `SOLVER_TIERS` の既定値を変更。unittest で既定値を固定 | `runner/loop.py`（`SOLVER_TIERS`）、`runner/tests/test_attempts.py` |
| 完了 | 役ごとにモデルと effort を指定する | 呼び出し回数の多い役と、判断の重い役で、モデルと考える量を分ける | `.env` の `LOOP_MODEL` と `LOOP_EFFORT` を bash 配列に積み、`--model` と `--effort` で渡す。effort の値は `case` で検査する。`.env` に行が無ければ 45 が追記する | `provision/bin/planner-run`、`critic-run`、`solver-claude`、`provision/45-agent-invoke.sh` |
| 完了 | ランナー本体を配置する | runner が自分の関門のコードを書き換えられないようにする | `install -o root -g root -m 644` で `/srv/loop/runner/loop.py` に置く。runner から書けないことと、`ast.parse` で構文が壊れていないことを assert する | `provision/25-runner.sh`、`provision/provision.sh` |
| 完了 | npm のピア依存解決の失敗を避ける | npm 10.9 が vitest の任意のピア依存を解決する途中で落ちる | `npm install` に `--legacy-peer-deps` を付けて自動解決を切る | `provision/35-node.sh` |
| 完了 | `plan refine` で改訂前の計画を残す | 改訂の失敗やプランナーのエスカレーションで、リンタを通っていた計画が消えない | Python。改訂前の提案を dict に控え、失敗時とエスカレーション時に `restore_proposal` で `out/` へ書き戻す。エスカレーションの本文は `.runner/refine-escalation.md` に残す。`unittest.mock` でテスト | `runner/loop.py`（`cmd_plan_refine` `restore_proposal`）、`runner/tests/test_refine.py` |
| 完了 | 要件から計画を作って実走する | 3役すべて Claude Code のまま、要件から緑まで届くことを確かめる。結果は 8/8 が1回目の試行で緑、テスト52件 | fixture と同じ題材の要件を書き、`plan bootstrap` → `plan refine --mode coverage` → `plan apply` → `run --all`。プランナー `claude-opus-5-5`、ソルバーとクリティック `claude-sonnet-5`、effort はすべて `medium` | ── |
| 一部完了 | smoke を Claude ソルバーで通す | 別 uid での起動、認証、非対話実行、書いたファイルの所有者を確かめる | smoke-solver、smoke-planner、smoke-critic、smoke-dom を箱で実行して通過。smoke-pytest は下の行で書き換える | `provision/bin/smoke-*` |
| 完了 | 批評が走らなかったときに clean と表示しない | 要件が `/srv/loop/human/in/REQUIREMENTS.md` に無いと、coverage が黙って飛ばされ、指摘ゼロとして扱われる | Python。飛ばしたモードを台帳（`CRITIQUE_SKIPPED`）と画面に出す。走ったモードが無ければ `no_critique_ran` が終了コード 1 で止める。`unittest.mock` でテスト | `runner/loop.py`（`run_critique` `no_critique_ran` `cmd_plan_refine` `cmd_critique`）、`runner/tests/test_refine.py` |
| 完了 | 35-node.sh が作ったファイルをコミットする | `.gitignore`、`index.html`、`vitest.config.mjs` が未コミットのまま残り、最初の `run --all` が dirty で止まる | bash。3つを書いた直後に、runner として `git add`、`git commit`、`git push` する。対象はこの3つだけに絞り、変更が無ければ何もしない。20-layout.sh の最初のコミットと同じ形 | `provision/35-node.sh` |
| 完了 | `/srv/loop` を root 所有にする | 親ディレクトリが runner 所有なので、runner は `bin/` や `runner/` を改名して差し替えられる。sudoers はパスで許可しているので、偽の `solver-run` を他の uid で実行できる | bash。`/srv/loop` を `root:root 755` にする。`repo.git/` と `project/` は無いときだけ root が runner 所有の空ディレクトリを作り、runner が `git init --bare` と `git clone` で中身を作る。走行ログ用に `logs/` を runner に渡す。runner が `/srv/loop` に書けないことと、渡した場所に書けることを assert する | `provision/20-layout.sh` |
| 完了 | `index.html` を TypeScript の計画にだけ見せる | Python の計画でも「環境の事実」としてクリティックに渡り、クリティックはこれを根拠に的外れな指摘を出す | Python。箱は計画の言語を知らないので、置く側（35-node.sh）は変えない。`environment_facts` が、TypeScript のときだけ `index.html` の説明を出し、Python のときはディレクトリの一覧からも `index.html` と `vitest.config.mjs` を外す。unittest で両方の言語を確かめる | `runner/loop.py`（`environment_facts`）、`runner/tests/test_brief.py` |
| 完了 | smoke-pytest を書き換える | ソルバーに pytest を実行させる smoke は、Bash を許さない方針では必ず失敗する | bash。ソルバーに pytest の実行を頼み、実行の痕跡（ソルバー所有の `tests/__pycache__` と `.pytest_cache`）が無いことを確かめる。レポートは手書きできるので証拠にしない。痕跡が無いのにレポートがあれば、手書きとして失敗にする。テストファイルが書き換えられていないことと、ソルバーのプロセスが残っていないことも確かめる | `provision/bin/smoke-pytest` |
| 完了 | venv の判定に pip の有無を加える | `python` だけある壊れた venv を「作成済み」とみなし、`pip` が見つからず止まる | bash。判定を `bin/python` と `bin/pip` の両方の有無にする。片方しか無ければ `rm -rf` で消して、runner が作り直す | `provision/30-python.sh` |
| 完了 | L8 違反を減らす | 2回の計画づくりで2回とも L8 に引っかかり、プランナーの呼び出しが1回ずつ増えた。違反はどれも境界のケースで、`[]` `{}` `None` のような具体的な値が、判定の正規表現に入っていなかった | Python。`CONCRETE` に空のコレクション（`[]` `{}` `()`）と `True` `False` `None` を足す。TypeScript の小文字の定数は、英語の単語と見分けがつかないので足さない。ブリーフの L8 の説明も合わせる。unittest で受理と拒否の両方を確かめる | `runner/loop.py`（`CONCRETE` `BOOTSTRAP_RULES`）、`runner/tests/test_linter.py` |
| 完了 | プランナーにソルバーの道具を伝える | CONTEXT.md に「テストを走らせてから終える」と書かれる。ソルバーは Bash を持たないので、実行を試みて断られる | Python。`environment_facts` が、使うバックエンド（`SOLVER_TIERS`）にコマンドを実行できるもの（`SOLVERS_THAT_RUN_COMMANDS`、今は codex だけ）が無ければ、「ソルバーはコマンドを実行できない。CONTEXT.md にテストを走らせろと書くな」と伝える。unittest でバックエンドの組み合わせごとに確かめる | `runner/loop.py`（`SOLVERS_THAT_RUN_COMMANDS` `environment_facts`）、`runner/tests/test_brief.py` |
| 完了 | 利用上限への到達を失敗と区別する | 3役が同じサブスクリプションの枠を共有する。上限到達が Halt → エスカレーションに化け、エスカレーションがさらにプランナー呼び出しで枠を使う連鎖を止める | Python。Claude Code のエラーのドキュメントにある文面（`You've hit your ... limit`、`Request rejected (429)`、`Repeated 529 Overloaded` など）を正規表現で見分ける。3役の呼び出しを `run_agent` で包み、該当すれば 15分待って同じ呼び出しをやり直す（最大24回）。待ち切ったら Halt ではない `QuotaExhausted` で、エスカレーションせずに終了コード 5 で止まる。文面は実物では未確認。unittest で確かめる | `runner/loop.py`（`QUOTA_PATTERNS` `QUOTA` `run_agent` `call_solver` `call_planner` `call_critic` `main`）、`runner/tests/test_quota.py` |
| 完了 | 消費量を台帳に残す | どの役が枠を使ったかを run 後に読める | bash と Python。3役の起動スクリプトに `--output-format json` を付ける。ランナーの `run_agent` が JSON を読み、使ったモデル・`usage`・推定費用・所要時間・ターン数を台帳の `USAGE` に書く。呼び出し側には結果の文だけを返すので、呼び出し側は変えない。JSON でない出力はそのまま通す。unittest で確かめる | `provision/bin/planner-run`、`critic-run`、`solver-claude`、`runner/loop.py`（`unwrap_result` `record_usage` `run_agent`）、`runner/tests/test_quota.py` |
| 未着手 | run 8 の計画を Claude ソルバーで回す | run 5（Codex）、run 6（ローカル 9B）と同じ物差しで比較する | run 8 の要件で `plan bootstrap` から回し、ステップごとの試行回数と所要時間を記録する | `docs/HANDOFF.md` |
| 未着手 | ドキュメントを Claude 単独構成に揃える | 役の表、資格情報の置き場、未決事項、tier の例を現構成に合わせる | 各ドキュメントを書き直す。箱の作り方に、実行する場所（Git Bash / PowerShell / 箱）、鍵の名前、公開鍵の流し込み、クローンでの配置を反映する。走行ログの置き場を `/srv/loop/logs/` に、run の退避を保守ユーザーの `sudo mv` に書き換える | `README.md`、`docs/ARCHITECTURE.md`、`docs/RUNNER_SPEC.md`（§4-4-1、§11-1）、`docs/LOCAL_SOLVER.md`、`provision/README.md`、`host/README.md`、`provision/70-local-solver.sh`、`.gitignore` |
| 未着手 | 英語コメントの日本語化 | 日本人である私が読めるようにする | 英語のコメントを、意味を変えずに日本語へ書き直す | 英語コメントアウト全般 |

### 着手前に決めること

- サブスクリプションを1つで回すか、役ごとに分けるか
  - 1つなら、役ごとに資格情報を分けても枠は分かれない
  - 人間の対話作業とも同じ枠を取り合う
- codex とローカルモデルのバックエンドを残すか
  - 残す場合、Claude 単独構成を既定にして任意の tier として扱う

## 更新履歴

- 2026/09/25: 作業の表に実装方法の列を追加
- 2026/09/25: 作業の表に状態の列を追加し、実走で見つかった作業を追加
- 2026/09/24: ソルバーのツールを Read Write Edit に決定
- 2026/09/23: 具体的な作業の表と着手前に決めることを追加