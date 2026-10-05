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
| `<src-dir>` | ソルバーがコードを書くディレクトリ。作業ツリーの根からの相対パス（`Assets/Source` など） |
| `<unity-project-dir>` | Unity で開いたことのあるプロジェクトのディレクトリ。`Assembly-CSharp.csproj` があるところ |
| `<tests-dir>` | ソルバーがテストを書くディレクトリ。`<src-dir>` を含まず、`<src-dir>` に含まれない場所 |
| `<you>` | Windows のユーザー名 |
| `<mirror-dir>` | `loop-pull` が写したプロジェクトのディレクトリ |
| `<port>` | ダッシュボードが待ち受けるポート。既定は 8443 |

## 走らせる（ホストでも箱でも）

| やりたいこと | コマンド |
|---|---|
| 要件から最後まで走らせる | `loop go <requirements>` |
| TypeScript で走らせる | `loop go <requirements> --language typescript` |
| C++ で走らせる | `loop go <requirements> --language cpp` |
| 状態を見る | `loop status` |
| ログを追う（Ctrl-C で抜けても走行は続く） | `loop log` |
| 止まったところから続ける | `loop continue` |
| 止める | `loop stop` |
| いまの作業を JSON で見る | `loop now` |

`loop go` は、要件の配置、`plan bootstrap`、`plan refine`、`plan apply`、`run --all` を順に裏で流す。
ホストで打つときは、`<requirements>` にホストのファイルのパスをそのまま渡せる。

## 止まったとき（ホストでも箱でも）

止まった理由と次の手は、`loop status` とログの最後の行に出る。

改訂の上限まで回してもクリティックの指摘が残るか、改訂の途中でプランナーが判断を返すと止まる。的外れな指摘や、プランナーの問いに答える指摘を書き換えてから `loop continue` を流すと、
書き換えた指摘でプランナーが1回だけ計画を直し、適用して走らせる。何も書き換えなければ、そのまま適用する。
ダッシュボードの「クリティックの指摘」からも、書き換えと続行ができる。

| やりたいこと | コマンド |
|---|---|
| 途中で止まったステップを最後の緑に戻す | `loop raw reset <step>` |
| 計画をリンタにかける | `loop raw validate` |
| 適用待ちの提案を見る | `loop raw plan show` |
| 適用待ちの提案を適用して続ける | `loop continue` |
| クリティックの指摘を見る | `loop findings` |
| 残った指摘を1件書き換える（番号は0から） | `echo '{"mode": "<mode>", "index": <n>, "title": "<題>", "evidence": "<根拠>"}' \| loop findings set` |
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
| 書き込みの柵の場所を決めて用意する | `loop project init <project> --src <src-dir> --tests <tests-dir>` |
| 作ってあるプロジェクトの柵の場所を変える | `loop project layout <project> --src <src-dir> --tests <tests-dir>` |
| Unity の参照アセンブリを送る（ホストだけ） | `loop-unity-refs <project> <unity-project-dir>` |
| 切り替える | `loop project use <project>` |
| `loop-project.sh` より前に作った箱に名前を付ける | `loop project adopt <project>` |

走行中は切り替えられない。先に `loop stop` か、終わるのを待つ。

## 既存のリポジトリのブランチで作業させる

GitHub とやり取りするのはホストだけだ。箱には GitHub の資格情報を置かない。ホストと箱のあいだは
`loop-runner`（ホストの `~/.ssh/config` に書いた接続先）で push と pull をする。
ホストの git のコマンドは、どれも `<clone-dir>` で打つ。

### 取り込む

ホストで1行打つ。下の1から4をまとめて流し、切り替えが終わるまで待つ。クローンは `C:\dev\roop-engin\projects\<project>` に置く。
ダッシュボードの「取り込み」タブでも同じことができる。

```bash
loop-import <project> <repo-url> <branch> [<base-branch>] [--src <src-dir>] [--tests <tests-dir>]
```

`--src` と `--tests` は書き込みの柵の場所で、省くと `src` と `tests`。

手で打つときは次の順に流す。

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
loop-pull
```

`C:\dev\roop-engin\projects\<project>` の `<branch>` が箱の先まで進む。早送りできないときは `NOTE` が
出て、箱の成果は `loop/<branch>` にだけ入る。そのときは `<clone-dir>` で自分で合わせる。

```bash
git merge loop/<branch>
```

### PR に出す（ホスト）

ダッシュボードで予定レビューを承認すると、`<branch>-pr` から `<base-branch>` への PR が出る。
先に `loop-pull` を流して、写しを箱の先まで進めておく。仕組みと前提は
[host/dashboard/README.md](../host/dashboard/README.md) の「プルリクエスト」にある。

`loop-import` より前に取り込んだプロジェクトは、親ブランチを1回だけ記録する。

```bash
git -C <clone-dir> config branch.<branch>.loopBase <base-branch>
```

手で出すときは次のとおり。`<branch>` には、箱が置いた環境のファイル（`conftest.py`、`index.html`、`vitest.config.mjs`、
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

- ソルバーが書けるのは、柵の2つのディレクトリの下だけだ。既定は `src/` と `tests/`。コードが別の場所にあるリポジトリ（Unity なら `Assets/` の下）は、`loop project layout` で場所を決める
- 自前の `conftest.py`、`index.html`、`vitest.config.mjs` があると、プロビジョニングは上書きせずに止まる
- `.gitignore` に `node_modules/` があると `35-node.sh` が止まる
- 依存パッケージは入らない。箱にあるのは pytest と vitest だけだ
- 既存のファイルでは、スタブは計画が挙げた名前の宣言だけを差し替える。既存のテストは凍結されるので、振る舞いを変えるとそのテストが回帰として落ちる

## C++ で作業させる

```bash
loop go <requirements> --language cpp
```

箱は標準の C++17 のロジックだけを g++ と GoogleTest で確かめる。DXライブラリや Windows の API は箱に無い。

- `DxLib.h` や `windows.h` のように、標準でもリポジトリのものでもないヘッダを include するファイルはビルドから外す。外したファイルは計画づくりのブリーフに並ぶ
- 描画、入力、音を扱うファイルもソルバーが書けるが、箱ではビルドも実行もしない。人が Visual Studio でビルドして画面を確かめる
- `DxLib.h` を include するファイルがあるか、要件が DXライブラリに触れていれば、プランナーに DXライブラリの主な関数の早見表を渡す
- 新しい `.cpp` と `.h` は Visual Studio のプロジェクト（`.vcxproj`）に入らない。CONTEXT.md の一覧を見て、人が足す
- ソースは ASCII だけで書かせる。画面に日本語を出すなら、Visual Studio でソースの文字コードの扱い（`/utf-8` など）を決めてから人が書き足す
- C++ の既存コードの宣言はまだプランナーに渡らない。スタブはソルバーが書き、ランナーは既存のファイルの残りが変わっていないかを確かめない

## 見る

| やりたいこと | 場所 | コマンド |
|---|---|---|
| 成果物をホストに写す | ホスト | `loop-pull` |
| VS Code で箱に入る | ホスト | `loop-dev` |
| 端末で箱に入る | ホスト | `ssh loop-dev` |

## ダッシュボード（ホスト）

ダッシュボードは `127.0.0.1:8443` だけで待ち受ける。「いまの作業」「クリティックの指摘」「ステップ」は
箱から直接読み、それ以外の欄はホストの写し（`C:\dev\roop-engin\project`）を読む。トークン消費の
グラフは、`C:\dev\roop-engin` の下の写しを全部読み、箱の今の回を重ねる。

### 起動する

| やりたいこと | コマンド |
|---|---|
| 設定ファイルを作る（初回だけ） | `copy host\dashboard\config.example.json host\dashboard\config.json` |
| 写しを最新にする | `loop-pull` |
| 起動する | `loop-dashboard` |
| 写しの場所を指定して起動する | `python host\dashboard\server.py --project <mirror-dir>` |
| ポートを変えて起動する | `python host\dashboard\server.py --project <mirror-dir> --port <port>` |
| 止める | 起動した端末で Ctrl-C |

起動したら <http://127.0.0.1:8443> を開く。`config.json` が無くても進捗の画面は使える。

`loop-dashboard` は写しのディレクトリが無いと起動しない。初回は先に `loop-pull` を流す。計画がまだ
無いあいだは、「いまの作業」と「ステップ」だけが埋まる。

「いまの作業」は5秒ごとに `ssh loop-dev loop now` を `BatchMode=yes` で流す。鍵にパスフレーズが
あるなら、先に ssh-agent に載せておく。

### 画面から操作する

画面の「操作」で、次をコマンドなしで済ませる。

| やりたいこと | 欄 | 同じことをするコマンド |
|---|---|---|
| 要件から走らせる | 走行 | `loop go <requirements> --language <言語>` |
| 止まったところから続ける | 走行 | `loop continue` |
| 止める | 走行 | `loop stop` |
| 途中で止まったステップを最後の緑に戻す | 走行 | `loop raw reset <step>` |
| プロジェクトを切り替える | プロジェクト | `loop project use <project>` |
| 役ごとのモデルと effort を変える | 役のモデル | `/etc/loop/<役>.env` の `LOOP_MODEL` と `LOOP_EFFORT` を書き換える |
| 写しを最新にする | ホストの写し | `loop-pull` |
| 既存リポジトリのブランチを取り込む | 取り込み | `loop-import <project> <repo-url> <branch> [<base-branch>]` |

箱は、これらを `loop dash` の1本で受ける。箱を更新したら、`provision.sh` を流し直して sudoers の
規則を入れる。

### スマホや他の PC から見る

| やりたいこと | コマンド |
|---|---|
| tailnet に公開する | `tailscale serve --bg --https=8443 http://127.0.0.1:8443` |
| 公開の状態を見る | `tailscale serve status` |
| 公開をやめる | `tailscale serve --https=8443 off` |
| 名簿に書くログイン名を見る | `tailscale status` |

公開するには、`config.json` の `remote` に公開名（`<machine>.<tailnet>.ts.net:8443`）と、見てよい人の
Tailscale のログインを書く。どちらかが無ければ、tailnet からの要求はすべて拒まれる。
`tailscale funnel` は使わない。インターネット全体に公開される。

リモートから、予定レビューの承認、走行の開始、プロジェクトの切り替え、モデルの変更、写しの更新、
取り込みはできない。差し戻し、エスカレーションへの回答、走行の停止と続行、止まったステップのやり直しはできる。

設定の詳細: [host/dashboard/README.md](../host/dashboard/README.md)

## 箱を保守する

### 更新する（箱）

スクリプトやランナーを更新したら、`loop update` を流す。このリポジトリを pull し、プロビジョニングを
流し直す。何度流しても同じ状態になる。走行中とプロジェクトの切り替え中は断る。
ホストからは `host\loop.cmd` で `loop update` と打つ。

pull の後のコミットが、最後にプロビジョニングが通ったコミット（`/etc/loop/provisioned`）と同じなら、
プロビジョニングを飛ばす。プロビジョニングが途中で落ちたときは記録が古いままなので、次の
`loop update` が流し直す。コミットが同じでも流し直すときは `--force` を付ける。

```bash
loop update
loop update --force
```

`loop update` は最初のプロビジョニングが置く。それより前は次の2行を流す。

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
- ダッシュボードの設計と設定: [host/dashboard/README.md](../host/dashboard/README.md)
- `loop.py` の動詞と終了コード: [RUNNER_SPEC.md](RUNNER_SPEC.md)
