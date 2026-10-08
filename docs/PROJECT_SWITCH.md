# プロジェクトを切り替えて走らせる

既存のリポジトリのブランチを新しいプロジェクトとして箱に置き、`loop go` まで走らせる流れ。
Unity のプロジェクトを C# で走らせる場合を例にする。コマンドはどれもホストの PowerShell で打つ。
個々のコマンドの説明は [COMMANDS.md](COMMANDS.md) にある。

`<...>` は自分の値に置き換える。記号の意味は [COMMANDS.md](COMMANDS.md) の表と同じ。

## 全体の流れ

1. 今のプロジェクトの成果を引き取る
2. ホストでクローンし、作業ブランチを切る
3. 箱に受け皿を作る
4. ブランチを箱へ送る
5. Unity の参照を送る
6. 切り替える
7. 走らせる
8. 成果を引き取り、PR に出す

## 1. 今のプロジェクトの成果を引き取る

```powershell
loop-pull
```

ダッシュボードが読むのは、今のプロジェクトの写し（`C:\dev\roop-engin\project`）だけだ。切り替えると、
前のプロジェクトの予定レビューは画面から消える。前のプロジェクトの PR を出すなら、ここでダッシュボードから承認する。

切り替えても前のプロジェクトは消えない。箱の中では退避され、`loop project use` で戻せる。

## 2. クローンして作業ブランチを切る

```powershell
git clone <repo-url> C:\dev\roop-engin\projects\<project>
git -C C:\dev\roop-engin\projects\<project> switch --no-track -c <branch> origin/<base-branch>
git -C C:\dev\roop-engin\projects\<project> config branch.<branch>.loopBase <base-branch>
```

- クローンは `C:\dev\roop-engin\projects\<project>` に置く。`loop-pull` とダッシュボードの PR はこの場所を見る
- `loopBase` は PR の宛先になる。記録が無いと、ダッシュボードから PR を出せない

## 3. 箱に受け皿を作る

```powershell
loop project init <project> --branch <branch> --src <src-dir> --tests <tests-dir>
```

柵の場所はここで決める。Unity なら `--src Assets/Source --tests Assets/Tests/Editor/Loop` のように、`Assets/` の下を指す。

2 から 4 と 6 は、`loop-import` の1行か、ダッシュボードの「取り込み」タブでまとめて流せる。柵の場所は
`--src` と `--tests`（タブでは「コードの柵」と「テストの柵」）で渡す。渡さないと既定の `src/` と `tests/` で
プロビジョニングが走り、あとで `loop project layout` を流すともう1回走る。まとめて流したときは、5 の参照を
切り替えの後に送る。今のプロジェクトに送った参照は、その場でつながる。

```powershell
loop-import <project> <repo-url> <branch> <base-branch> --src <src-dir> --tests <tests-dir>
```

## 4. ブランチを箱へ送る

```powershell
git -C C:\dev\roop-engin\projects\<project> push loop-runner:/srv/loop/projects/<project>/repo.git <branch>
```

## 5. Unity の参照を送る

```powershell
loop-unity-refs <project> <unity-project-dir>
```

`<unity-project-dir>` は、Unity で一度開いて `Assembly-CSharp.csproj` ができているディレクトリだ。同じリポジトリを
取り込んだ別のプロジェクトのクローンでもよい。`<base-branch>` で Unity の版やパッケージが変わっているなら、
新しいクローンを Unity で開いてから、それを渡す。

## 6. 切り替える

```powershell
loop project use <project>
```

- 初回はプロビジョニングが走るので、数分かかる
- 走行中は切り替えられない。先に `loop stop` か、終わるのを待つ
- ブランチが push されていなければ、何も動かさずに止まる

## 7. 走らせる

```powershell
loop go <requirements> --language csharp
```

`--language` を付けないと Python で計画が立つ。C# のプロジェクトでは、プランナーが「テストが pytest しか無い」と
言って計画を書かずに止まる。

持ち込む先は `--framework` で選ぶ。省くと言語の既定になり、C# なら `unity` だ。ダッシュボードでは「走行」の
「持ち込む先」で選ぶ。

要件に曖昧なところがあると、プランナーは計画を書かずに `ESCALATE.md` を返す。質問に答える形で要件を書き足し、
`loop go` を流し直す。

```powershell
loop status
```

止まった理由は `loop status` の「最後」の行に出る。

## 8. 成果を引き取り、PR に出す

```powershell
loop-pull
loop-dashboard
```

全ステップが緑になったら、`loop-pull` で写しを箱の先まで進めてから、ダッシュボードで予定レビューを承認する。
承認すると `<branch>-pr` から `<base-branch>` への PR が出る。

写しが箱より遅れていると、ダッシュボードに予定レビューが出ない。

要件が「テストは完了後に削除する」と決めている場合、ループはテストを消さない。PR を出す前に人が消す。

## 前のプロジェクトに戻る

```powershell
loop project list
loop project use <project>
```

`loop project list` の `*` が今のプロジェクトだ。一度作ったプロジェクトに戻るときは、プロビジョニングは走らず、
退避していた作業ツリーと計画がそのまま戻る。
