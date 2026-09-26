# host/ — Windows 側の付属物

サンドボックスの外、ホスト側に置くもの。`provision/` がディストロの中を作るのに対し、
こちらは**ディストロを起動して繋ぐところ**を担当する。

WSL2 では起動と生存管理がホスト側の責務になった（`RUNNER_SPEC.md` §1-6）ので、
ここが無いとサンドボックスはそもそも上がってこない。

| ファイル | 置き場所 | 役割 |
|---|---|---|
| `loop-dev.cmd` | `C:\Users\<you>\bin\loop-dev.cmd`（PATH の通った場所） | ディストロ起動 → sshd 待機 → VS Code Remote-SSH 起動 |
| `loop.cmd` | このリポジトリのまま（`host` を PATH に足す） | ディストロ起動 → sshd 待機 → 箱の `loop` コマンドを実行 |
| `wsl-keepalive.vbs` | このリポジトリのまま（タスクが絶対パスで参照する） | VM を**窓を出さずに**生かし続ける。下の keepalive タスクの実体 |
| `loop-pull.cmd` | このリポジトリのまま | **すべての** `repo*.git` を run ごとのミラーに引く。**VHDX を失っても残る唯一の複製** |
| `loop-dashboard.cmd` | このリポジトリのまま | 進捗、エスカレーション、予定レビューを扱うGUIを起動（`127.0.0.1:8443`） |

**ASCII のみで書くこと。** PowerShell 5.1 と cmd.exe は BOM 無し UTF-8 を ANSI として
読むため、日本語コメントを入れると行継続として誤解釈され、変数が黙って null になる
（`provision/README.md` §3-6）。

## 箱を操作する

`loop.cmd` は、ディストロを起動して sshd を待ち、`ssh -t loop-dev loop <引数>` を流す。
引数は箱の `loop` コマンドと同じ（`provision/README.md` §2-10）。

`loop` だけで打てるよう、このリポジトリの `host` ディレクトリをユーザーの PATH に足す
（手順は `docs/COMMANDS.md`）。コピーして置くと、pull しても更新が届かない。

```cmd
loop go C:\path\to\requirements.md
loop status
loop log
```

`go` に渡した要件がこの機械のファイルなら、先に保守ユーザーのホームへ
`loop-requirements.md` として送る。走行は箱の中で続くので、窓を閉じてもよい。
続きは `loop log` で追う。

## オペレーターGUI

`dashboard/` は、ホストへ取得したプロジェクトの台帳を読み、進捗、エスカレーション、
全 Green 後の予定レビューをブラウザで扱うローカルGUI。次の順で同期して起動する。

```cmd
host\loop-pull.cmd
host\loop-dashboard.cmd
```

その後、<http://127.0.0.1:8443> を開く。

スマホや他のPCから見るときは `tailscale serve` を前に置く（`dashboard/README.md`）。
**サーバはループバックのまま**で、LAN には一切出さない。

```powershell
tailscale serve --bg --https=8443 http://127.0.0.1:8443
```

公開すると端末によってできることが変わる。**リモートは「駄目だ」と言えるが「良い」とは
言えない** ── 予定レビューの承認と成果物の起動はこの機械の前だけ。画面を見られない端末が
「遊べた」と記録できるなら、*機械には画面が確認できないから人間に訊く* という関門が
意味を失う。差し戻し・エスカレーションへの回答・停止はどこからでもできる。

予定されたプレイテストは失敗ではないため、`ALL_GREEN` から「予定レビュー」を生成し、
`ESCALATION.md` から生じる異常時のエスカレーションとは画面上・記録上ともに区別する。
回答は `dashboard/.state/decisions.jsonl` に対象ID付きで保存され、古い画面から別の要求へ
回答することはできない。

成果物の起動は `dashboard/config.json` に人間が書いたIDの許可リストだけを使う。計画、
ソルバー出力、HTTP入力をコマンドとして実行する経路はない。詳しい設定とテスト方法は
`dashboard/README.md`。

## リポジトリに入っていないもの

手で作る必要があるが、内容が短いのでここに手順だけ置く。

### keepalive スケジュールタスク（必須）

これが無いと VM が約60秒のアイドルで停止し、SSH が使えなくなる
（`provision/README.md` §3-1）。**タスクは `wsl.exe` を直接起動せず、
`wsl-keepalive.vbs` 越しに起動する**（理由は下の「窓を出さない」）。

```powershell
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\wscript.exe" `
  -Argument '"<このリポジトリ>\host\wsl-keepalive.vbs"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "WSL-keepalive-Ubuntu-24-04" `
  -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
```

`schtasks /create` ではなく `ScheduledTasks` モジュールを使うのは、
**`ExecutionTimeLimit` を無制限（`PT0S`）にできるのがこちらだけ**だから。
`schtasks` の既定は72時間で、越えるとタスクスケジューラが keepalive を止める。

`-RestartCount 3` により、keepalive が異常終了しても1分後に自動で戻る
（`.vbs` が `wsl.exe` の終了コードをそのまま返すのはこのため）。ただし
**ログオフで落ちた場合は戻らない** ── トリガが onlogon なので次のログオンまで待つ。

`wsl --shutdown` を打つと巻き添えで死ぬ。自動復帰を待たずに戻すなら
`schtasks /run /tn "WSL-keepalive-Ubuntu-24-04"`。

#### 窓を出さない

以前はタスクが `wsl.exe` を直接起動していた。`/rl limited` の対話タスクなので
**コンソール窓が開き、タスクバーに残る**。中身は `sleep infinity` なので何も表示されず、
見た目はただの空のターミナルで、**それを閉じると VM が落ちる。**
しかも onlogon なので自動では戻らない。1〜2時間ループを流している最中に、
誤クリック1回で全部止まるという壊れ方をする。

`WScript.Shell.Run(..., 0, True)` なら、同じ wsl.exe クライアントセッションを
窓なしで保持できる。アイドル判定が数えているのは窓ではなく**クライアントセッション**なので、
生存条件は変わらない（切り替え中も VM は落ちなかった。実測 2026-08-19）。

生きているかは窓ではなくプロセスで見る:

```powershell
Get-CimInstance Win32_Process -Filter "Name='wsl.exe' or Name='wscript.exe'" |
  Select-Object ProcessId, ParentProcessId, Name
```

`wscript.exe` の親が `svchost.exe`（タスクスケジューラ）になっていれば正しい。

### `~/.ssh/config`

Git Bash で追記する。鍵は `provision/README.md` §2-1 で作った `loop-dev` と `loop-runner`。
`<保守ユーザー>` と `<you>` は実際の名前に置き換える。

```bash
cat >> ~/.ssh/config <<'EOF'
Host loop-dev
    HostName 127.0.0.1
    Port 2222
    User <保守ユーザー>
    IdentityFile C:\Users\<you>\.ssh\loop-dev
    IdentitiesOnly yes
    # WSL は再作成でホスト鍵が変わるため、未知なら自動受け入れ
    StrictHostKeyChecking accept-new
    ServerAliveInterval 30
    ServerAliveCountMax 6

Host loop-runner
    HostName 127.0.0.1
    Port 2222
    User runner
    IdentityFile C:\Users\<you>\.ssh\loop-runner
    IdentitiesOnly yes
EOF
```

`loop-dev` は保守ユーザーでの対話と VS Code Remote-SSH に使う。`loop-runner` は
`repo*.git` を引くためだけに使う（git 経路専用、鍵も別）。`runner` はプロビジョニングで
作られるので、`loop-runner` はプロビジョニングが終わるまで通らない。

成果物は `loop-pull.cmd` を使わずに直接クローンしてもよい:

```bash
git clone loop-runner:/srv/loop/repo.git <置き場所>
```

### ホスト側のミラー（バックアップ）

ループの成果物は3段で外に出る。**段が上がるほど失いにくくなる:**

| 段 | どこへ | 何から守るか |
|---|---|---|
| 1 | `project` → `/srv/loop/repo.git` | `reset` / `clean`。**同じ VHDX の中**なので、それ以上は守らない |
| 2 | `repo.git` → `<MIRRORROOT>\project`、過去run → `runs\run-NNN` | **VHDX の消失**。ここで初めて別のディスクに乗る |
| 3 | ミラー → GitHub など | ホストの故障。やるなら**鍵はホストだけが持つ** |

段1 はランナーが自動でやる（`loop.py` の `publish()`、GREEN と `plan apply` の直後）。
段2 が `loop-pull.cmd`。**引数も事前のクローンも要らない** ── サンドボックスにある
`repo*.git` を全部列挙し、無ければクローン、有れば fetch する。
1件でも clone / fetch / reset / clean / fast-forward に失敗すれば、その場で非ゼロ終了する。
不変アーカイブをすべて確認してから最後にライブミラーへ進むため、アーカイブ同期に失敗した
状態でライブを作り直さない。成功済みの独立アーカイブは巻き戻さないが、部分成功を
「done」と表示してはならない。

```text
/srv/loop/repo.runN.git  ->  <MIRRORROOT>\runs\run-NNN  （不変。ff のみ）
/srv/loop/repo.git       ->  <MIRRORROOT>\project        （現行。毎回作り直す）
```

`<MIRRORROOT>` は `loop-pull.cmd` 冒頭の `set "MIRRORROOT=..."` で決まる。使う前に自分の置き場所に書き換える。

**run ごとにディレクトリを分けるのは整頓ではなく保存のため。** どの run も
`step-S1`…`step-S11` という同じタグ名を作るので、1つのクローンに引くと `--force` で
前の run のタグを上書きするしかなく、`main` も動かせば**古い run のコミットを指す ref が
1つも残らない**。到達不能なオブジェクトは `git gc` が消し、gc は普通のコマンドの中で
勝手に走る。**誰も見ていない時点でバックアップが消える。**

サンドボックス側の退避は、保守ユーザーが `sudo mv` で `project.<名前>` /
`repo.<名前>.git` に改名する（`provision/README.md` §2-11）。ホスト側は `runs\` の下に
まとめ、ライブ成果物だけを `project` に置く。各保存先は独立したGitリポジトリである。

`project`（現行 run）は毎回 `reset --hard` と `clean` で作り直す。run ごとに
`repo.git` は新しい root コミットから始まるので ff できないため。
**したがって `project\` に自分の物を置かないこと** ── 維持されるのではなく作り直される。
その回の内容は次の pull までに `runs\run-NNN` として捕まっているので、失われるものは無い。

**押すのではなく引くのは意図的**。サンドボックスは生成されたコードを実行する場所なので、
外に届く認証情報をその中に置かない。段3 をやる場合も同じで、GitHub の鍵は
**VM に入れず**、ホストのミラーから押す。

### `.wslconfig`

`C:\Users\<you>\.wslconfig`。必須項目は `provision/README.md` §2-2。
**変更の反映には `wsl --shutdown` が必要**で、それは keepalive を殺すので必ずセットで扱う。

## 更新履歴

- 2026/09/26: 箱の `loop` コマンドを呼ぶ `loop.cmd` を追加
- 2026/09/26: `~/.ssh/config` の鍵の名前を `loop-dev` / `loop-runner` に、keepalive とミラーのパスを置き換え前提の書き方に変更
