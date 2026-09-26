# よく使うコマンド

打つ場所は2つある。

| 場所 | 何か |
|---|---|
| ホスト | Windows の cmd、PowerShell、Git Bash。`host\` のスクリプトと git を使う |
| 箱 | `ssh loop-dev` で入った保守ユーザーの端末。`loop` コマンドを使う |

ホストの `loop` は `host\loop.cmd` のこと。中身は `ssh -t loop-dev loop <args>` で、ディストロの
起動と sshd の待機も先に済ませる。だから下の `loop` の行は、ホストと箱のどちらで打っても同じに動く。

ホストで `loop` だけで打つには、`host` ディレクトリをユーザーの PATH に足す。PowerShell で1回だけ
打ち、端末を開き直す。`loop-pull` と `loop-dashboard` も同じく名前だけで打てるようになる。

```powershell
$hostDir = "<repo-dir>\host"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$userPath;$hostDir", "User")
```

VS Code のターミナルは、VS Code 本体が起動したときの PATH を受け継ぐ。足したあとは、ターミナルでは
なく VS Code を全部閉じて起動し直す。Git Bash は拡張子を補わないので、`loop` ではなく `loop.cmd` と打つ。

PATH に足していなければ、`<repo-dir>` でパスごと呼ぶ。cmd なら `host\loop.cmd <args>`、
PowerShell なら `.\host\loop.cmd <args>`、Git Bash なら `./host/loop.cmd <args>`。

`<...>` は自分の値に置き換える。

| 記号 | 何を入れるか |
|---|---|
| `<repo-dir>` | ホストにクローンした Loop Engine のリポジトリ |
| `<requirements>` | 要件のファイルのパス。手元だけで使うものは `<repo-dir>\requirements\local\` に置く |
| `<step>` | ステップの ID（`S3` など） |
| `<project>` | 箱の中でのプロジェクトの名前。英小文字、数字、`.` `_` `-` |
| `<repo-url>` | 対象のリポジトリの GitHub の URL |
| `<clone-dir>` | ホストで対象のリポジトリをクローンしたディレクトリ |
| `<branch>` | Loop Engine に作業させるブランチの名前 |
| `<base-branch>` | 元にするブランチの名前（`main` など） |
| `<pr-branch>` | PR に出すブランチの名前 |
| `<admin-user>` | 箱の保守ユーザーの名前 |
| `<you>` | Windows のユーザー名 |

## 走らせる（ホストでも箱でも）

| やりたいこと | コマンド |
|---|---|
| 要件から最後まで走らせる | `loop go <requirements>` |
| TypeScript で走らせる | `loop go <requirements> --language typescript` |
| 状態を見る | `loop status` |
| ログを追う（Ctrl-C で抜けても走行は続く） | `loop log` |
| 止まったところから続ける | `loop continue` |
| 止める | `loop stop` |
| いまの作業を JSON で見る | `loop now` |

`loop go` は、要件の配置、`plan bootstrap`、`plan refine`、`plan apply`、`run --all` を順に裏で流す。
ホストで打つときは、`<requirements>` にホストのファイルのパスをそのまま渡せる。

## 止まったとき（ホストでも箱でも）

止まった理由と次の手は、`loop status` とログの最後の行に出る。

| やりたいこと | コマンド |
|---|---|
| 途中で止まったステップを最後の緑に戻す | `loop raw reset <step>` |
| 計画をリンタにかける | `loop raw validate` |
| 適用待ちの提案を見る | `loop raw plan show` |
| 適用待ちの提案を適用して続ける | `loop continue` |
| エスカレーションへの改訂案をプランナーに書かせる | `loop raw plan propose --step <step>` |
| 計画を批評だけする | `loop raw critique` |

`loop raw` は `loop.py` をそのまま runner として前面で呼ぶ。動詞の一覧は `loop raw --help`。

## プロジェクトを切り替える（ホストでも箱でも）

| やりたいこと | コマンド |
|---|---|
| 一覧を見る（`*` が今のもの） | `loop project list` |
| 今のプロジェクトの名前 | `loop project current` |
| 空のプロジェクトを用意する | `loop project init <project>` |
| 既存リポジトリのブランチを受け入れる用意をする | `loop project init <project> --branch <branch>` |
| 切り替える | `loop project use <project>` |
| `loop-project.sh` より前に作った箱に名前を付ける | `loop project adopt <project>` |

走行中は切り替えられない。先に `loop stop` か、終わるのを待つ。

## 既存のリポジトリのブランチで作業させる

GitHub とやり取りするのはホストだけだ。箱には GitHub の資格情報を置かない。ホストと箱のあいだは
`loop-runner`（ホストの `~/.ssh/config` に書いた接続先）で push と pull をする。
ホストの git のコマンドは、どれも `<clone-dir>` で打つ。

### 取り込む

1. 作業用ブランチを切る（ホスト）

    ```bash
    git clone <repo-url> <clone-dir>
    cd <clone-dir>
    git switch -c <branch> <base-branch>
    ```

    もうクローンしてあるなら、`<clone-dir>` で最後の1行だけ打つ。

2. 受け皿を作る（箱）

    ```bash
    loop project init <project> --branch <branch>
    ```

3. ブランチを箱へ送る（ホスト）

    ```bash
    git push loop-runner:/srv/loop/projects/<project>/repo.git <branch>
    ```

4. 切り替えて走らせる（箱）

    ```bash
    loop project use <project>
    loop go <requirements>
    ```

    `use` は、ブランチが push されていなければ何も動かさずに止まる。初回はプロビジョニングが
    走るので数分かかる。

### 成果を引き取る（ホスト）

```bash
git pull loop-runner:/srv/loop/projects/<project>/repo.git <branch>
```

### PR に出す（ホスト）

`<branch>` には、箱が置いた環境のファイル（`conftest.py`、`index.html`、`vitest.config.mjs`、
`.gitignore` への追記）と計画（`plan/`）がコミットされている。PR には要らないので、別のブランチで
消してから出す。`<branch>` そのものは消さずに残す。箱はこれからもそこで作業する。

```bash
git switch -c <pr-branch> <branch>
git rm -r -q --ignore-unmatch plan conftest.py index.html vitest.config.mjs
git checkout origin/<base-branch> -- .gitignore
git commit -m "chore: Loop Engineの環境のファイルを除く"
git push origin <pr-branch>
```

`<base-branch>` に `.gitignore` が無ければ、`git checkout` の行は失敗する。そのときは代わりに
`git rm -q .gitignore` を打つ。箱が作ったファイルなので、消せば元に戻る。

そのあと GitHub で `<pr-branch>` から `<base-branch>` へ PR を出す。

### 取り込むときの条件

- コードは `src/`、テストは `tests/` に置かれている必要がある
- 自前の `conftest.py`、`index.html`、`vitest.config.mjs` があると、プロビジョニングは上書きせずに止まる
- `.gitignore` に `node_modules/` があると `35-node.sh` が止まる
- 依存パッケージは入らない。箱にあるのは pytest と vitest だけだ
- スタブは `files_write` のファイルを丸ごと上書きする。要件は、新しいファイルを足す形で書く

## 見る

| やりたいこと | 場所 | コマンド |
|---|---|---|
| 成果物をホストに写す | ホスト | `loop-pull` |
| ダッシュボードを開く | ホスト | `loop-dashboard` のあと <http://127.0.0.1:8443> |
| スマホからダッシュボードを見る | ホスト | `tailscale serve --bg --https=8443 http://127.0.0.1:8443` |
| VS Code で箱に入る | ホスト | `loop-dev` |
| 端末で箱に入る | ホスト | `ssh loop-dev` |

ダッシュボードの「いまの作業」と「ステップ」は箱から直接読む。それ以外の欄はホストの写しを読む。
`loop-dashboard` は写しが無いと起動しないので、初回は先に `loop-pull` を流す。

## 箱を保守する

### 更新する（箱）

スクリプトやランナーを更新したら、pull してからプロビジョニングを流し直す。何度流しても同じ状態になる。

```bash
sudo git -C /opt/loop-engine pull
cd /tmp && sudo ADMIN_USER=<admin-user> bash /opt/loop-engine/provision/provision.sh
```

### 配管を確かめる（箱）

smoke は、箱の配管が繋がっているかを最小の往復で確かめる検査だ。箱を作ったあと、資格情報を
入れ替えたあと、プロビジョニングのスクリプトや起動スクリプトを直したあとに流す。
runner として流す。

| smoke | 確かめること |
|---|---|
| `smoke-solver` | ランナーがソルバーを別の uid で起動できるか。認証、端末なしの実行、書いたファイルの所有者 |
| `smoke-pytest` | ソルバーが pytest を実行できないこと |
| `smoke-planner` | プランナーが動き、計画にもコードにも直接触れないこと |
| `smoke-critic` | クリティックが起動でき、書いたファイルを読み戻せること |
| `smoke-dom` | happy-dom の UI の関門が、生きた UI で通り、壊れた UI で落ちること |
| `smoke-page` | 開発サーバで `index.html` を開くと `src/main.ts` の `start` まで届くこと |

```bash
sudo -u runner /srv/loop/bin/smoke-solver
sudo -u runner /srv/loop/bin/smoke-pytest
sudo -u runner /srv/loop/bin/smoke-planner
sudo -u runner /srv/loop/bin/smoke-critic
sudo -u runner /srv/loop/bin/smoke-dom
sudo -u runner /srv/loop/bin/smoke-page
```

### keepalive を起こし直す（ホスト）

VM が落ちて keepalive が戻らないときは、cmd でタスクを起こし直す。

```bat
schtasks /run /tn "WSL-keepalive-Ubuntu-24-04"
```

## リポジトリのテスト（ホスト）

ホストにクローンした Loop Engine のリポジトリの根で流す。Python 3 が要るので、普段使いの WSL
ディストロ（箱の `Ubuntu-24.04` ではないもの）から流す。箱は Windows のパスを見せないので、
そこでは流せない。

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
