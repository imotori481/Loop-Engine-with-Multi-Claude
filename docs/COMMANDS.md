# よく使うコマンド

打つ場所は2つある。

| 場所 | 何か |
|---|---|
| ホスト | Windows の cmd か PowerShell。`host\` のスクリプトを使う |
| 箱 | `ssh loop-dev` で入った保守ユーザーの端末。`loop` コマンドを使う |

ホストの `loop` は `host\loop.cmd` のこと。PATH の通った場所（`C:\Users\<you>\bin` など）に
置けば `loop` だけで呼べる。中身は `ssh -t loop-dev loop <引数>` で、ディストロの起動と
sshd の待機も先に済ませる。だから下の `loop` の行は、ホストと箱のどちらで打っても同じに動く。

## 走らせる

| やりたいこと | コマンド |
|---|---|
| 要件から最後まで走らせる | `loop go <要件>.md` |
| TypeScript で走らせる | `loop go <要件>.md --language typescript` |
| 状態を見る | `loop status` |
| ログを追う（Ctrl-C で抜けても走行は続く） | `loop log` |
| 止まったところから続ける | `loop continue` |
| 止める | `loop stop` |
| いまの作業を JSON で見る | `loop now` |

`loop go` は、要件の配置、`plan bootstrap`、`plan refine`、`plan apply`、`run --all` を順に裏で流す。
ホストで打つときは、要件にホストのファイルのパスをそのまま渡せる。

```bat
loop go C:\work\requirements.md
```

## 止まったとき

止まった理由と次の手は、`loop status` とログの最後の行に出る。

| やりたいこと | コマンド |
|---|---|
| 途中で止まったステップを最後の緑に戻す | `loop raw reset <ステップ>` |
| 計画をリンタにかける | `loop raw validate` |
| 適用待ちの提案を見る | `loop raw plan show` |
| 適用待ちの提案を適用して続ける | `loop continue` |
| エスカレーションへの改訂案をプランナーに書かせる | `loop raw plan propose --step <ステップ>` |
| 計画を批評だけする | `loop raw critique` |

`loop raw` は `loop.py` をそのまま runner として前面で呼ぶ。動詞の一覧は `loop raw --help`。

## プロジェクトを切り替える

| やりたいこと | コマンド |
|---|---|
| 一覧を見る（`*` が今のもの） | `loop project list` |
| 今のプロジェクトの名前 | `loop project current` |
| 空のプロジェクトを用意する | `loop project init <名前>` |
| 既存リポジトリのブランチを受け入れる用意をする | `loop project init <名前> --branch <ブランチ>` |
| 切り替える | `loop project use <名前>` |
| `loop-project.sh` より前に作った箱に名前を付ける | `loop project adopt <名前>` |

走行中は切り替えられない。先に `loop stop` か、終わるのを待つ。

## 見る

| やりたいこと | 場所 | コマンド |
|---|---|---|
| 成果物をホストに写す | ホスト | `host\loop-pull.cmd` |
| ダッシュボードを開く | ホスト | `host\loop-dashboard.cmd` のあと <http://127.0.0.1:8443> |
| スマホからダッシュボードを見る | ホスト | `tailscale serve --bg --https=8443 http://127.0.0.1:8443` |
| VS Code で箱に入る | ホスト | `loop-dev` |
| 端末で箱に入る | ホスト | `ssh loop-dev` |

ダッシュボードの「いまの作業」と「ステップ」は箱から直接読む。それ以外の欄はホストの写しを読む。
`loop-dashboard.cmd` は写しが無いと起動しないので、初回は先に `host\loop-pull.cmd` を流す。

## 箱を保守する

スクリプトやランナーを更新したら、pull してからプロビジョニングを流し直す。何度流しても同じ状態になる。

```bash
sudo git -C /opt/loop-engine pull
cd /tmp && sudo ADMIN_USER=<保守ユーザー> bash /opt/loop-engine/provision/provision.sh
```

配管を確かめる smoke は runner として流す。

```bash
sudo -u runner /srv/loop/bin/smoke-solver
sudo -u runner /srv/loop/bin/smoke-pytest
sudo -u runner /srv/loop/bin/smoke-planner
sudo -u runner /srv/loop/bin/smoke-critic
sudo -u runner /srv/loop/bin/smoke-dom
sudo -u runner /srv/loop/bin/smoke-page
```

VM が落ちて keepalive が戻らないときは、ホストでタスクを起こし直す。

```bat
schtasks /run /tn "WSL-keepalive-Ubuntu-24-04"
```

## リポジトリのテスト

ホストのリポジトリの根で、普段使いの WSL などの Python 3 から流す。

```bash
python3 -m unittest discover -s runner/tests
python3 -m unittest discover -s host/dashboard/tests
```

## 詳しい説明

- 走らせ方と止まる場面: [provision/README.md](../provision/README.md) §2-10
- プロジェクトの切り替え: [provision/README.md](../provision/README.md) §2-11
- ホスト側のスクリプトと SSH の設定: [host/README.md](../host/README.md)
- ダッシュボード: [host/dashboard/README.md](../host/dashboard/README.md)
- `loop.py` の動詞と終了コード: [RUNNER_SPEC.md](RUNNER_SPEC.md)
