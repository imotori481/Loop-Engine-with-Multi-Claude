# loop サンドボックス（WSL2）のプロビジョニング

RUNNER_SPEC.md の実行環境（§1）を再現するための手順とスクリプト。

実行基盤は **WSL2 の `Ubuntu-24.04` ディストロ**。VirtualBox は使わない（理由は §6）。

作り直すときはこの手順をなぞる。**§3 の落とし穴は全部、踏んだか実機で確認したもの**で、
どれも「一見成功して、後から静かに壊れる」種類なので飛ばさないこと。

---

## 1. 構成

| | |
|---|---|
| ディストロ | WSL2 `Ubuntu-24.04`（systemd 有効）。**普段使いのディストロとは別に作る** |
| CPU / RAM | `.wslconfig` で配分する（例: 6 プロセッサ / 8GB / swap 4GB） |
| ディスク | ext4 on VHDX（`sparseVhd=true`） |
| ネットワーク | `networkingMode=NAT` + `localhostForwarding=true` |
| SSH | ディストロ内 sshd が **2222 番で listen**。Windows からは `127.0.0.1:2222` |
| Windows ドライブ | **マウントしない**（`automount enabled=false`） |
| Windows 実行ファイル | **起動不可**（`interop enabled=false`） |
| WSLg | **無効**（`.wslconfig` `guiApplications=false`。§3-8） |
| このリポジトリ | 箱の中の `/opt/loop-engine` にクローンする。スクリプトはそこから実行する |

アカウント:

| ユーザー | uid | sudo | SSH | 用途 |
|---|---|---|---|---|
| 保守ユーザー（既定ユーザー） | 1000 | あり | 鍵のみ | プロビジョニングと保守。VS Code Remote-SSH はここに繋ぐ |
| `runner` | 1001 | **なし** | 鍵のみ | ループの実行主体。ホストからの git pull を受ける |
| `solver` | 1002 | **なし** | **不可**（`AllowUsers` に載せない + `DenyUsers`） | 実装を書く Claude Code。ファイルの読み書きだけを許す |
| `planner` | 1003 | **なし** | **不可** | 受け入れ基準を書く Claude Code。コードには触れない（BOOTSTRAP 1-1） |
| `critic` | 1004 | **なし** | **不可** | 計画の欠陥を指摘する Claude Code。**計画もテストも読めない** |

保守ユーザーは、ディストロを作るときに決めた既定ユーザーをそのまま使う。
**名前はこのリポジトリでは決め打ちにしていない。** スクリプトは `ADMIN_USER` で受け取り、
既定は `maint`。実際の名前と違うときは `ADMIN_USER=<保守ユーザー>` を必ず渡す。
渡さないと `10-users.sh` が「`maint` が無い」で止まる。

`critic` の uid が分かれている理由は、他の3人とは毛色が違う。solver と planner は
「書けるものを制限する」ための分離だが、critic は**読めるものを制限する**ための分離で、
役の価値そのものがそこにある ── 計画を読める critic は「全基準を満たしている」と答え、
テストを読める critic は「テストは通る」と答える。どちらも真で、どちらも無価値。

ホスト側の付属物（詳細と再作成手順は `host/README.md`）:

| もの | 場所 | 役割 |
|---|---|---|
| `loop-dev` ランチャ | `C:\Users\<you>\bin\loop-dev.cmd`（原本は `host/loop-dev.cmd`） | ディストロ起動 → sshd 待機 → VS Code Remote-SSH 起動 |
| SSH 設定 | `C:\Users\<you>\.ssh\config` の `Host loop-dev` / `Host loop-runner` | `127.0.0.1:2222` / 鍵のみ |
| keepalive タスク | タスクスケジューラ `WSL-keepalive-Ubuntu-24-04` | **これが無いと VM がアイドルで落ちる**（§3-1） |
| リソース設定 | `C:\Users\<you>\.wslconfig` | メモリ/CPU/NAT/sparseVhd/WSLg |

---

## 2. 作り直す手順

各手順の見出しに、打つ場所を書いてある。

| 場所 | 何か |
|---|---|
| **Git Bash** | Windows 側の Git for Windows に付いてくる bash。鍵の生成と ssh に使う |
| **PowerShell** | Windows 側の PowerShell。`wsl` コマンドと Windows の設定に使う |
| **箱** | ディストロの中の bash。保守ユーザーでログインして打つ |

`wsl` コマンドは箱の中には無い。箱の中で `wsl --shutdown` を打っても何も起きない。

### 2-1. 鍵を作る（Git Bash）

保守ユーザー用とループ用（`runner`）で鍵を分ける。前者は人が対話で使い、
後者はホストが `repo.git` を git pull するためだけに使う。
PowerShell ではなく Git Bash で作る（理由は §3-5）。

```bash
ssh-keygen -t ed25519 -f ~/.ssh/loop-dev    -N '' -C loop-dev
ssh-keygen -t ed25519 -f ~/.ssh/loop-runner -N '' -C loop-runner
# 必ず検証する。空パスフレーズで復号できなければ失敗している
ssh-keygen -y -f ~/.ssh/loop-dev    -P ''
ssh-keygen -y -f ~/.ssh/loop-runner -P ''
```

鍵の名前は `loop-dev` と `loop-runner` を使う。以降の手順と `host/README.md` の
`~/.ssh/config` はこの名前を前提にしている。

### 2-2. ディストロを作る（PowerShell → 箱）

**PowerShell**:

```powershell
wsl --install -d Ubuntu-24.04     # 既定ユーザー（保守ユーザー）を対話で作る
wsl -l -v                         # Ubuntu-24.04 / Running / 2 であることを確認
```

**箱**: `/etc/wsl.conf` を次の内容にする。`<保守ユーザー>` は実際の名前に置き換える
（`<` と `>` も消す）。`[boot] systemd=true` が無いと `systemctl` が使えず、
sshd の管理も 50-lockdown.sh の検証も成立しない。

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true

[user]
default=<保守ユーザー>

[automount]
enabled=false

[interop]
enabled=false
appendWindowsPath=false
EOF
```

**PowerShell**: `.wslconfig` を開く。ファイルが無ければメモ帳が新しく作る。

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

次の3つは隔離の前提:

```ini
[wsl2]
localhostForwarding=true
networkingMode=NAT
guiApplications=false
```

- `localhostForwarding=true`: `127.0.0.1:2222` で sshd に届く
- `networkingMode=NAT`: `mirrored` だとディストロから Windows の localhost サービスに到達できてしまう
- `guiApplications=false`: 既定は true で、切らないと `/mnt/wslg` 経由の経路が開いたままになる（§3-8）

残り（`memory` / `processors` / `swap` / `sparseVhd` / `autoMemoryReclaim`）は
性能配分の話で、隔離には関わらない。`host/README.md` を参照。

**`/etc/wsl.conf` と `.wslconfig` の変更は `wsl --terminate` では反映されない。**
PowerShell で `wsl --shutdown` を打ち、keepalive タスクを再起動する（§3-2）。

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04 -- cat /etc/wsl.conf    # 起動し直し、中身を確かめる
```

### 2-3. sshd を 2222 で立てる（箱）

```bash
sudo apt-get update && sudo apt-get install -y openssh-server
sudo tee /etc/ssh/sshd_config.d/10-loop-dev.conf >/dev/null <<'EOF'
Port 2222
ListenAddress 0.0.0.0
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers <保守ユーザー>
X11Forwarding no
EOF
sudo systemctl enable --now ssh
```

ここでは `AllowUsers` が保守ユーザーだけになる。`runner` は 50-lockdown.sh が
`00-loop.conf` 側で足す（**足す順番に意味がある。§3-4**）。

22 ではなく 2222 を使うのは、Windows 側の 22 と衝突させないためと、
`localhostForwarding` で `127.0.0.1:2222` にそのまま出るようにするため。

### 2-4. 保守ユーザーの公開鍵を流し込む（PowerShell → Git Bash）

箱には `/mnt/c` が無いので、公開鍵は `wsl` の標準入力で渡す。

**PowerShell**:

```powershell
Get-Content "$env:USERPROFILE\.ssh\loop-dev.pub" |
  wsl -d Ubuntu-24.04 -- sh -c 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
```

**Git Bash**: `host/README.md` の `~/.ssh/config` を書き、入れるか確かめる。

```bash
ssh loop-dev
```

config を書く前に確かめるなら、鍵を明示する。鍵の名前が既定の `id_ed25519` では
ないので、`-i` を付けないと `Permission denied (publickey)` になる。

```bash
ssh -i ~/.ssh/loop-dev -p 2222 <保守ユーザー>@127.0.0.1
```

### 2-5. Node と Claude Code（箱）

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm i -g @anthropic-ai/claude-code
```

Claude Code は apt のパッケージではない。`apt install claude` は通らない。

**必ず `sudo npm i -g`（prefix が `/usr`）にすること。** ユーザーローカル prefix に入れて
`.bashrc` で PATH を足す形にすると、Ubuntu の `.bashrc` が非対話シェルで早期 return する
ため、`sudo -u solver` で起動したエージェントから見えなくなる。nvm も同じ理由で使えない。

interop を切ると Windows 側の `npm`/`node` が PATH から消えるので、
ディストロ内にシステムワイドで入れる必要がある。

Codex CLI（`@openai/codex`）は、計画の `solver_tiers` に `codex` を挙げるときだけ入れる。

### 2-6. このリポジトリをクローンする（箱）

```bash
sudo git clone -b develop <このリポジトリの https URL> /opt/loop-engine
```

箱には GitHub の鍵を置かないので、https で取る。改行コードは `.gitattributes` で LF に
なるので変換は要らない。スクリプトの更新は pull で取り込む:

```bash
sudo git -C /opt/loop-engine pull
```

### 2-7. runner の公開鍵を流し込む（PowerShell）

```powershell
Get-Content "$env:USERPROFILE\.ssh\loop-runner.pub" |
  wsl -d Ubuntu-24.04 -- sh -c 'mkdir -p /tmp/loop-provision && cat > /tmp/loop-provision/loop-runner_ed25519.pub'
```

**箱の中での名前は `loop-runner_ed25519.pub` で固定。** `15-authkeys.sh` がこの名前を
決め打ちで読む。Windows 側の鍵の名前とは関係ない。無ければ
`FATAL: /tmp/loop-provision/loop-runner_ed25519.pub not found` で止まる。

`/tmp` は VM の再起動で消える。プロビジョニングの直前に流し込む。

### 2-8. プロビジョニング（箱）

```bash
cd /tmp && sudo ADMIN_USER=<保守ユーザー> bash /opt/loop-engine/provision/provision.sh
```

`cd /tmp` は省かない（§3-14）。何度流しても同じ状態になるので、スクリプトを
pull したあとも同じ行を流す。

`05-isolation.sh` が WSL 隔離（Windows パス非マウント、WSLg、systemd、NAT）を、
`35-node.sh` が Node 側の凍結を、`40-perms.sh` が solver 視点の権限モデルを、
`45-agent-invoke.sh` が資格情報の柵を assert する。1つでも落ちたら異常終了する。
`05-` を最初に走らせるのは、隔離が効いていないディストロには
**プロビジョニングする意味が無い**（以降の全ステップが成功しつつ何も意味しなくなる）ため。

`35-node.sh` は vitest と happy-dom を `/srv/loop/node` に入れて凍結し、
`project/node_modules` からシンボリックリンクで見えるようにする。**画面の無い機械で
UI を検査するための土台**で、主張の検算は:

```bash
sudo -u runner /srv/loop/bin/smoke-dom
```

生きた UI は3件通り、run 7 と同じ形の UI（ボタンが最初から全部無効）は2件落ちる ──
`expected 0 to be greater than 0`。**両方向を見るのが要点**で、何でも通す関門は
働いている関門と見分けがつかない。

`35-node.sh` は最後に `smoke-page` を流す。開発サーバ（Vite）で `index.html` を開いたとき、
`src/main.ts` の `start` まで届くことを確かめる。計画には開発サーバの応答を確かめる
条件を書かせないので、要件の「開発サーバで開ける」はここで確かめる。

```bash
sudo -u runner /srv/loop/bin/smoke-page
```

Vite はインラインのモジュールスクリプトを `/index.html?html-proxy&index=0.js` に切り出し、
変換後の HTML には `/src/main.ts` が現れない。だから見るのは HTML ではなく、切り出された
モジュールの中身だ。キャッシュは作業場所に書き、凍結したツールチェーンには書かない。

### 2-9. 資格情報を入れる（箱）

3役はそれぞれ別のファイルから資格情報を読む。各ファイルは、その役の uid だけが読める
（`root:<役> 640`）。

| 役 | ファイル |
|---|---|
| solver | `/etc/loop/solver.env` |
| planner | `/etc/loop/planner.env` |
| critic | `/etc/loop/critic.env` |

役ごとにトークンを作る。表示されたトークンを控えてから、ファイルを開いて貼る。

```bash
sudo -u solver -H claude setup-token
sudo nano /etc/loop/solver.env
```

埋める行:

| 行 | 値 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN=` | `claude setup-token` が表示したトークン |
| `LOOP_MODEL=` | 使うモデル。空ならアカウントの既定。例: planner は `claude-opus-5-5`、solver と critic は `claude-sonnet-5` |
| `LOOP_EFFORT=` | `low` / `medium` / `high` / `xhigh` / `max`。空なら既定 |

planner と critic も同じ手順で埋める。3役とも同じサブスクリプションの利用枠を使う。
トークンが空の役があると、プロビジョニングの最後に `still unauthenticated` として名前が出る。

配管を確かめる:

```bash
sudo -u runner /srv/loop/bin/smoke-solver
sudo -u runner /srv/loop/bin/smoke-pytest
sudo -u runner /srv/loop/bin/smoke-planner
sudo -u runner /srv/loop/bin/smoke-critic
```

`smoke-pytest` は、ソルバーが pytest を実行**できない**ことを確かめる。

### 2-10. 走らせる（箱）

要件を人間の受け渡し口に置き、計画を起こして回す。

```bash
sudo install -o root -g humanw -m 644 <要件>.md /srv/loop/human/in/REQUIREMENTS.md
L="sudo -u runner python3 -u /srv/loop/runner/loop.py"
$L plan bootstrap                  # TypeScript なら --language typescript
$L plan refine                     # critic の指摘をプランナーへ戻す
$L plan apply
$L run --all 2>&1 | sudo -u runner tee -a /srv/loop/logs/<名前>.log
```

**走行ログは `/srv/loop/logs/` に書く。** `/srv/loop` の直下には runner も保守ユーザーも
書けない。`tee` も `sudo -u runner` で起こす ── パイプの先は保守ユーザーとして動くので、
そのままでは `Permission denied` になる。

### 2-11. run を退避する（箱）

次の題材に移る前に、今の run を別名に移す。`/srv/loop` は root 所有なので、
改名は保守ユーザーが `sudo` で行う。runner にはできない。

```bash
sudo mv /srv/loop/project  /srv/loop/project.<名前>
sudo mv /srv/loop/repo.git /srv/loop/repo.<名前>.git
cd /tmp && sudo ADMIN_USER=<保守ユーザー> bash /opt/loop-engine/provision/provision.sh
```

プロビジョニングが空の `project` と `repo.git` を作り直す。ホストの `loop-pull.cmd` は
`repo.<名前>.git` も含めて全部引く（`host/README.md`）。

### 2-12. スナップショット（PowerShell）

VirtualBox のスナップショットに相当するのは `wsl --export`。

```powershell
wsl --shutdown                                                   # export には停止が必要
wsl --export Ubuntu-24.04 D:\wsl-backup\loop-base.tar
# 復元: wsl --import Ubuntu-24.04-restore D:\wsl\restore D:\wsl-backup\loop-base.tar
```

**`wsl --shutdown` を使ったら keepalive タスクを再起動すること**（§3-2）。
export は数 GB になるのでリポジトリには入れない（`.gitignore` に `*.tar`）。

---

## 3. 落とし穴

### 3-1. keepalive タスクが無いと VM がアイドルで落ちる（最重要）

WSL2 は約60秒アイドルすると **VM ごと停止する**。sshd も一緒に落ちる。
`WSL-keepalive-Ubuntu-24-04`（ログオン時に `wsl -d Ubuntu-24.04 -u root --exec /usr/bin/sleep infinity`）
がこれを抑えている。**起動は `host/wsl-keepalive.vbs` 経由**で、窓を出さない
（定義と理由は `host/README.md`）。

実測で確認した事実（2026-08-17）:

- `.wslconfig` の `vmIdleTimeout` は **`-1` でも大きな正の値でも効かない**
- **アイドル判定は `wsl.exe` クライアントセッションだけを数える。SSH のトラフィックは数えない。**
  そのため VS Code Remote-SSH で接続中でも VM は落ちる（接続途中で落ちて
  「リモートを開いています」で止まる、が実際に起きた）
- デタッチしたバックグラウンドプロセスも VM を保持しない

タスクを作る手順は `host/README.md` にある。**`schtasks /create` では作らないこと**
── `ExecutionTimeLimit` の既定は72時間で、それを外せるのは `Register-ScheduledTask` の側だけ。

**タスクから `wsl.exe` を直接起動すると窓が出る**。中身は `sleep infinity` で
何も映らないので空のターミナルに見え、閉じると VM ごと落ちる。`.vbs` 越しに起動して
窓を消してある。

**帰結: この基盤では無人での長時間ループは回せない。** keepalive はログオンセッションに
紐づくので、ログオフすれば VM は落ちる。無人運用が必要になったら Hyper-V に移す（§5）。
窓を消しても、スリープ・再起動・ログオフで落ちることは変わらない。

### 3-2. `wsl --shutdown` を使うと keepalive が死ぬ

keepalive プロセスは `wsl --shutdown` で `STATUS_CONTROL_C_EXIT` で落ちる。
タスクには `RestartCount 3` を付けてあるので**1分後に自力で戻る**が、
待たずに戻すなら明示的に再実行する。**走行中は `wsl --shutdown` を使わない**
（VM が落ちれば走行中のステップは死ぬ）:

```powershell
schtasks /run /tn "WSL-keepalive-Ubuntu-24-04"
```

`.wslconfig` の変更には `--shutdown` が要るので、そのときは必ずセットで行う。

### 3-3. iptables が入っていない

Ubuntu 24.04 の WSL イメージには `iptables` も `nft` も無い。
`60-egress.sh` は自分で `apt-get install -y iptables` する（nft バックエンドで動く）。

さらに **WSL2 の VM はアイドルで落ちるので、iptables ルールは頻繁に消える。**
`iptables-persistent` による復元が実質必須。`60-egress.sh` の末尾を参照。

### 3-4. sshd の drop-in は「先に読んだ値が勝つ」。ただし `AllowUsers` は例外

sshd は各キーワードについて **最初に見た値**を採用する。`PasswordAuthentication` や
`Port` のような単一値のキーワードを後から `99-*.conf` で上書きしようとしても**負ける**。
エラーは出ないので、設定を書いたのに効いていない状態に気づけない。

→ `50-lockdown.sh` は `00-loop.conf` に書く（`10-loop-dev.conf` より先に読まれる）。
そして `sshd -T` で**実効値を検証する**。効いていなければ異常終了する。

**`AllowUsers` / `DenyUsers` はリスト値で、drop-in をまたいで累積する。**
`10-loop-dev.conf` の `AllowUsers <保守ユーザー>` と `00-loop.conf` の
`AllowUsers <保守ユーザー> runner` は、順序に関係なく合算される。
帰結として重要なのは逆方向で、**後から書く drop-in は許可を広げることしかできない。**
誰かを外すには、その名前を書いているファイル自体を直す必要がある。

さらに `sshd -T` の出力形式が罠になる。**1ユーザーにつき1行**で出る:

```text
allowusers admin
allowusers runner
allowusers admin      ← 2つの drop-in に書かれているので重複して出る
denyusers solver
```

検証スクリプトで「最後の行」や「最初の行」だけを見ると誤判定する
（実際に `50-lockdown.sh` がこれで正しい設定を FAIL と判定した）。
全行を集めて集合として扱うこと。

### 3-5. PowerShell から `ssh-keygen -N ''` は空パスフレーズにならない

`-N '""'` も `--%` 経由の `-N ""` も、**空文字ではないパスフレーズ**として渡る。
生成自体は成功するので気づかない。症状は接続時の

```text
debug1: Server accepts key: ...
Permission denied (publickey,password).
```

サーバ側ログは `Connection reset by authenticating user runner [preauth]`。
**サーバは鍵を受理していて、クライアントが署名できずに切っている。**
`authorized_keys` を疑って時間を溶かす典型。

→ 鍵の生成は Git Bash で行い、`ssh-keygen -y -f <key> -P ''` で必ず検証する。

### 3-6. `.ps1` に日本語コメントを書くと壊れる

PowerShell 5.1 は BOM 無し UTF-8 を ANSI として読む。日本語文字の末尾バイトが
バッククォート（行継続）と解釈され、**コメント行が次の行を飲み込み、変数が黙って null になる。**

→ `.ps1` / `.cmd` は ASCII のみで書く（`loop-dev.cmd` がそうなっている）か、
スクリプトを作らずインライン実行する。多段クォート（PowerShell → ssh → リモートシェル）も
崩れやすいので、複雑なものは base64 化して
`echo <b64> | base64 -d | bash` で渡すのが確実。

### 3-7. Git Bash から `wsl.exe` を叩くとパスが変換される

Git Bash 経由で `wsl -d Ubuntu-24.04 --exec /bin/true` を実行すると、
MSYS が `/bin/true` を Windows パスに変換して失敗する:

```text
execvpe(C:/Program Files/Git/usr/bin/bash) failed: No such file or directory
```

変換されるのはプログラム名だけでなく**引数の絶対パスも**で、
`/usr/bin/bash /tmp/x.sh` は両方やられる。変換そのものを止める:

```bash
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -u root bash /tmp/x.sh
```

`loop-dev.cmd` が `/usr/bin/...` で通っているのは、cmd から呼ばれていて MSYS を
経由しないため。`wsl` を使う手順を PowerShell に寄せているのもこのため。

### 3-8. WSLg が Windows 側への通り道を開けたままにする

`automount` と `interop` を切っても、**WSLg（Linux GUI アプリ対応）は生きている。**
`/mnt/wslg` に Windows 側で動くコンポジタと PulseAudio サーバへのソケットがあり、
パーミッションは誰でも読める:

```text
drwxrwxrwx  .X11-unix
srwxrwxrwx  PulseServer / PulseAudioRDPSink / PulseAudioRDPSource
```

つまり `solver` からもディスプレイ・音声・クリップボード連携の経路に届く。
`/etc/wsl.conf` にはこれを切る設定が無く、Windows 側の `.wslconfig` で閉じる:

```ini
[wsl2]
guiApplications=false
```

反映には `wsl --shutdown` が必要（→ keepalive 再起動、§3-2）。反映後の状態:

- `/mnt/wslg` 配下のソケットが**全て消える**（`find -type s` が 0 件）
- `/mnt/wslg/versions.txt` と `/doc` の overlay マウントも消える
- `/tmp/.X11-unix` は空、ログインシェルの `DISPLAY` / `WAYLAND_DISPLAY` / `PULSE_SERVER` も未設定

**ただし `/mnt/wslg` ディレクトリ自体は残る**（`run/user/<uid>` の空の骨組みだけ）。
そのためディレクトリの有無で判定すると誤検知する。`05-isolation.sh` は
**ソケットと `/mnt/wslg` 配下のマウント**を探して FAIL にしている。
意図して受け入れる場合は `ALLOW_WSLG=1` を付けて実行する
（この waiver は他のチェックには効かない）。

### 3-9. interop の binfmt ハンドラは切っても残る

`[interop] enabled=false` にしても `/proc/sys/fs/binfmt_misc/WSLInterop` は
登録されたまま（`enabled` / `interpreter /init` / `magic 4d5a`）。
**したがってハンドラの有無は隔離の証拠にならない。**

実際に効いていないことは実行して初めて分かる。root で `C:` を
drvfs マウントして `cmd.exe` を叩いた結果:

```text
rc=1  WSL ERROR: UtilAcceptVsock:273: accept4 failed 110
```

`/init` が Windows 側に繋げず、vsock の accept でタイムアウトしている。
つまり実行経路は死んでいる。

**ただし root は `mount -t drvfs C: /somewhere` でいつでも C: を持ち込める**
（実測で成功する）。automount を切ることは root に対する防御ではない。
効いているのは「`solver` は sudo を持たないのでマウントできない」という点で、
**隔離の実質は権限モデル（`40-perms.sh`）と同じ土台に乗っている。**

だから `05-isolation.sh` は binfmt の登録を FAIL にせず、
**Windows パスが1つもマウントされていないこと**を主チェックにしている。

### 3-10. `set -o pipefail` 下の `... | grep -q`

`grep -q` は最初のマッチで即終了する。すると上流が SIGPIPE で死に、
**pipefail がパイプライン全体を失敗扱いにする。マッチしているのに失敗する。**
検証スクリプトで踏むと「正しい設定を誤りと判定する」ので厄介。

→ 一度変数に取ってから判定する。`50-lockdown.sh` の末尾と `05-isolation.sh` の
`loopback0` チェックを参照。

### 3-11. runner は自分が所有しないファイルを chmod できない

ソルバーが作ったファイルは `solver` 所有になる。ランナーは書き込みフェンスを
**開けても閉じられない** ── `chmod` は所有者か root にしか通らず、`chown` には root が要る。
ランナーに root を渡すのは本末転倒（関門を強制する側が関門を外せてしまう）。

→ ディレクトリが runner 所有なら、中のファイルは**削除できる**。読んで・消して・
runner として書き直すと所有権が移る（`runner/loop.py` の `adopt()`）。バイト列は同一なので
git も凍結マニフェストも影響を受けず、特権を1つも増やさない。

### 3-12. `/srv/loop/brief` に setgid が無いとブリーフが読まれない

runner が書いたブリーフのグループが `runner` になり、`solver` から読めなくなる。
症状は `solver-run` の `exited 2` だけで、原因から遠い。

→ `2750` にする（`20-layout.sh` / `40-perms.sh`）。ランナー側でも書き込み直後に
明示的に `chgrp solverw` している。ディレクトリのビットに依存しない。

### 3-13. Codex の `apply_patch` はワークスペースのルートを経由して書く

`solver_tiers` に `codex` を挙げたときの話。`/srv/loop/project` が `755 runner:runner` だと、
`src/` に権限があっても**全ての編集が失敗する**。しかもエラーは対象ファイル名で
報告されるので `src/` の権限を疑わせる。`bubblewrap` が未導入だと、さらに手前の
「サンドボックス構築の失敗」として出る。

→ ルートを `3775`（setgid + **スティッキー**）にする。書けるだけでは穴で、
unlink と rename は親ディレクトリの権限で決まるため `conftest.py` を差し替えられてしまう
（= FREEZE の無効化）。スティッキーが「削除・改名は所有者のみ」に制限する。
`40-perms.sh` が「作れること」と「消せないこと」を両方 assert する。
併せて `apt install bubblewrap` を入れる（バンドル版へのフォールバックは不安定）。

### 3-14. プロビジョニングを 0700 のホームから走らせない

保守ユーザーのホームを cwd にしたまま流すと、内部の `sudo -u runner git ...` が

```text
fatal: failed to stat '/home/<保守ユーザー>': Permission denied
```

で落ちる。**スクリプトの問題ではなく cwd の問題**で、`sudo` は呼び出し元の作業
ディレクトリをそのまま渡し、`runner` は 0700 のホームを辿れない。
`solver-run` の `cd /srv/loop/project` が存在する理由とまったく同じ形の失敗。

→ `cd /tmp` してから流す（§2-8）。

### 3-15. `.pyc` は実行した uid の所有物になる

`tests/__pycache__` に solver 所有の `.pyc` が残ると、3-11 により
ランナーが `tests/` のモードを再適用できなくなる。

→ ランナーの pytest は `PYTHONDONTWRITEBYTECODE=1` で走らせ、
モード変更は `__pycache__` を除外する。

### 3-16. sticky ビットは「ディレクトリの所有者」も例外にする

`/srv/loop/planner/out` は 3770 `runner:plannerw`。sticky を付けた目的は
「planner が runner 所有のファイルを消せないようにする」ことだが、
**逆向きは塞がらない** ── sticky の削除条件は「ファイルの所有者**または**
ディレクトリの所有者」なので、runner は planner が書いたファイルを消せる。

これは事故ではなく必要な性質。`plan propose` は前回の提案を消してから
プランナーを呼ぶ（残っていると、書かれなかった古い提案が今回の提案として
適用されてしまう）。root を使わずにそれができるのはこの規則のおかげ。

**ディレクトリを書かれた場合は別。**`out/` に planner 所有のディレクトリがあると、
中身を消す権限は runner に無い。`clear_proposal()` は再帰せず、人間に投げて止まる。

### 3-17. `git reset --hard` は台帳も巻き戻す

`plan/ledger.jsonl` は GREEN のときに `git add -A` で commit されるため git 管理下にある。
`reset` の `git reset --hard HEAD` は台帳を**最後の緑まで戻し**、
破棄しようとしている試行の記録（ESCALATED を含む）を消してしまう。

→ `cmd_reset` は reset の前に台帳を読み、後で書き戻す。
追記専用の台帳が、よりによって失敗の記録だけを失うのは無いより悪い。

### 3-18. `tkinter` は別パッケージで、無いと import 時点で落ちる

Debian 系では tkinter が標準で入らない。`python3-tk` が無いと `import tkinter` が
`ModuleNotFoundError` になり、**テストがそれを間接的に import した時点で**
アサーションに到達する前に落ちる。ソルバーからは、頼まれた内容と何の関係もない失敗が
返り続けることになり、試行回数を全部そこで使う。`30-python.sh` が入れる。

**それでもランナーは Tk の画面を検証できない。** `DISPLAY` は無いので、確かめられるのは
GUI プログラムの**ロジック**だけ。画面を持つ題材は TypeScript（happy-dom）で書くか、
テスト可能なコアと `Tk` に触る薄い殻を別のステップに割る。
画面そのものの確認はホスト側の人間がやる（`host/dashboard` の予定レビュー）。

### 3-19. setgid のルートは、runner が作ったファイルまで solver に書かせる

3-13 でルートを `3775`（setgid + スティッキー）にした。setgid には**もう一つの効果**が
ある ── **runner がルートに作ったファイルも group が `solverw` になる。**
runner の umask は 002 なので `664` で落ち、solver が書ける。

ルートにあるのは作業物ではなく**防壁そのもの**である:

| ファイル | 何を決めているか |
|---|---|
| `conftest.py` | pytest が何を import できるか（`sys.path`） |
| `vitest.config.mjs` | DOM テストが何に対して走るか |
| `.gitignore` | `git status` が何を stray として報告するか（= `assert_touched()` の全部） |

**どれか1つでも書き換えられれば、FREEZE が見張っているファイルに一切触れずに
FREEZE を無効化できる。**

→ `40-perms.sh` がルート直下の runner 所有ファイルを `runner:runner 644` に揃え、
`conftest.py` / `.gitignore` / `vitest.config.mjs` を solver が書けないことを assert する。
各スクリプトは**自分が作ったファイルの mode を自分で設定する**（後続に閉じさせない）。

### 3-20. Node の凍結は Python の凍結と同じ形では効かない

Python は `sys.path` をランナーが握っているので、solver が何をどこに入れても
テストは `.venv/bin/pytest` の環境でしか走らない。**Node は違う** ── モジュール解決が
ファイルシステム上の位置で決まるので、`src/node_modules/` に何か置かれれば
そこから解決される。ネットワークは開いているので、これはレジストリ全体への通り道になる。

塞いでいるのは `.gitignore` の書き方1つ:

```text
/node_modules        ← ルートだけを無視する
node_modules/        ← 全ての深さで無視する（これを書くと穴が開く）
```

`assert_touched()` は `git status --untracked-files=all` で stray を見つける。
**git が無視するものは、この検査から見えない。** 先頭スラッシュで固定すれば、
凍結済みのシンボリックリンクだけが無視され、`src/node_modules` は未追跡として現れる。
`35-node.sh` は `node_modules/` 形式の行を見つけたら**進まずに落ちる**。

ツールチェーン本体を `/srv/loop/node` に置いて**プロジェクトの外**に出しているのは
このため。中に置くと `node_modules/` を無視するしかなくなる。

`35-node.sh` の `npm install` は `--legacy-peer-deps` を付ける。npm 10.9 は vitest の
任意の peer 依存を解決する途中で `Cannot read properties of null (reading 'edgesOut')`
で落ちる。

## 4. 未適用

`70-local-solver.sh` は**ソルバーをローカルモデルに差し替えると決めたときだけ**当てる
（`60-egress.sh` と同じ理由で `provision.sh` からは呼ばない）。手順と失敗表は
`docs/LOCAL_SOLVER.md`。sudoers は変更しない ── `solver-run` が持つ Runas(solver) の許可1つで
足り、バックエンドは同じ uid で exec されるため。

`60-egress.sh` は**意図的に実行していない**。ネットワークを全開にしたまま測ったところ、
環境を変える4経路はすべて既に塞がっている:

| 試みたこと | 結果 | 効いている機構 |
|---|---|---|
| `.venv/bin/pip install requests` | Permission denied | `.venv` が runner 所有・`go-w` |
| `pip install --user requests` | externally-managed-environment | PEP 668 |
| `--user` で入れたものを `.venv/bin/python` から import | 見えない | venv は user site を無視する |
| `apt-get install` | dpkg lock: are you root? | sudo なし |

3つ目が要。**テストは必ず `.venv/bin/pytest` で走らせること** ── 素の `python3` に変えると
user site が復活し、この防御だけが崩れる。

既定のソルバー（`solver-claude`）はコマンドを実行できないので、上の経路はそもそも
試せない。egress 制限が買うのは環境凍結ではなく**持ち込みと持ち出し**の遮断であり、
ループの前提条件ではない。当てるかどうかはその脅威をどう見るかで決める。

**当てるなら IP 固定ではなくプロキシで作ること。** ソルバーの宛先は Claude の API で、
CDN の後ろにあり複数アドレスに解決される。IP 固定は黙って陳腐化し、
WSL2 では VM が頻繁に止まるので規則の永続化が要る ──
**永続化した固定 IP は時限爆弾**（ローテーション後、原因の分かりにくい停止として出る）。

観測の手順（推測でドメインを並べないこと）:

```bash
# DROP ではなく LOG を1本入れて、実際に叩かれた宛先だけを採る
iptables -I OUTPUT 1 -m owner --uid-owner "$(id -u solver)" \
  -m conntrack --ctstate NEW -j LOG --log-prefix "LOOPOBS "
sudo -u runner /srv/loop/bin/smoke-solver
dmesg | grep LOOPOBS
```

### 認証

- **3役とも Claude のサブスクリプションのトークン**（`claude setup-token`）を使う。
  置き場は `/etc/loop/<役>.env`（`root:<役> 640`）。手順は §2-9
- 役ごとにファイルを分けるのは、ある役の資格情報が漏れても他の役の資格情報が渡らないようにするため。
  `45-agent-invoke.sh` が「各役は自分のファイルだけを読める」ことを assert する
- 3役は同じ利用枠を使う。上限に当たった呼び出しは、ランナーが待ってからやり直す
- `ANTHROPIC_API_KEY_CONSOLE=` は予備。トークンが空のときだけ従量課金の API キーを使う
- Codex バックエンドを使う場合、認証は `solver` 自身の ChatGPT ログインで、
  `/home/solver/.codex`（0700）に入る: `sudo -u solver -H codex login --device-auth`

---

## 5. Hyper-V へ移す条件

WSL2 でよいのは「人が張り付いている間だけ回す」用途に限られる（§3-1）。
次のどれかが必要になったら Hyper-V の VM に移す:

- 無人で数時間以上ループを回す（ログオフしても走り続ける）
- ホストの再起動を挟んで自動復帰する
- ループ実行中に Windows 側で重い作業をしても影響を受けない

ディストロ内の構成（`provision/` の 10〜60）は**そのまま持っていける**。
変わるのはホスト側の起動・接続まわりと、スナップショットの取り方だけ。

## 6. なぜ VirtualBox を使わないか

**Hyper-V が有効な Windows ホストでは VirtualBox が使えない。**
Hyper-V / WSL2 / Virtual Machine Platform / メモリ整合性 のいずれかが有効だと、
VirtualBox は VT-x を直接使えず NEM モード（Hyper-V の API 経由）で動く。
この状態で Linux ゲストは起動時に **2回に1回ハングした**:

```text
nmi_backtrace_stall_check: CPU 1: NMIs are not reaching exc_nmi() handler
last activity: 4294855847 jiffies ago
```

jiffies が 32bit ラップした異常値になるのはタイマー起因のサイン。
`--paravirtprovider` を `kvm` → `none` に変えても頻度が下がるだけで解消しなかった。
再現性が無く（インストールと初回起動は通り、スナップショット後の再起動で初めてハング）、
数時間ループを回す基盤としては使えない。

Hyper-V を無効化すれば VT-x に戻って安定するが、**WSL2 も Docker Desktop も動かなくなる**。
すでに Hyper-V が動いているなら、そちらがネイティブなので WSL2 に寄せるほうが筋が良い。

| | VirtualBox | WSL2 |
|---|---|---|
| 起動の安定性 | 2回に1回ハング | 安定 |
| 隔離 | 既定で隔離 | **既定では隔離されない**（`/mnt/c` と interop を明示的に切る必要がある） |
| 無人実行 | できる | **できない**（§3-1） |
| スナップショット | `VBoxManage snapshot`（差分・軽い） | `wsl --export`（全体・数GB） |
| ホストからの root | できない | **いつでもできる**（`wsl -u root`） |

最後の行は重要で、脅威モデルが片方向であることを意味する。守っているのは
「サンドボックスから Windows を守る」方向だけで、逆方向は守っていない。
RUNNER_SPEC §0-1 の「人間の作業環境はサンドボックスの中身に手を入れない」は機構ではなく規律である。

VirtualBox 構成の手順は `c4374f4` から拾える。

## 更新履歴

- 2026/09/26: 開発サーバで開けることを確かめる `smoke-page` を §2-8 に追加
- 2026/09/26: 手順を、実行する場所の明記、鍵の名前 `loop-dev` / `loop-runner`、公開鍵の流し込み、`/opt/loop-engine` からのプロビジョニング、Claude の資格情報、走行ログと run の退避に合わせて書き換え
