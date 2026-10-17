# AGENT_IO.md — 役どうしの受け渡し

プランナー、クリティック、ソルバーに何を渡し、何を受け取るかを述べる。上限値とブリーフの本文はコードにある。この文書は形と、どこで量が増えるかを示す。

## 受け渡しの形

役どうしが直接やりとりすることはない。受け渡しは、どれも次の順で進む。

1. ランナーがブリーフを1ファイルに組み立て、その役だけが読める場所に置く
2. 役の起動スクリプトが、ブリーフを `claude -p` の標準入力に流す
3. 役は作業場所にファイルを書く
4. ランナーがそのファイルを読み、確かめてから使う

呼び出しは毎回使い捨てになる。HOME を一時ディレクトリにし、`--no-session-persistence` で起動するので、前の呼び出しの記憶は残らない。1回の呼び出しの中では、エージェントがツールを使って何ターンも回る。

| 役 | 起動 | ブリーフの置き場 | 書く場所 | ツール |
|---|---|---|---|---|
| planner | `planner-run` | `/srv/loop/planner/brief/plan.md` | `/srv/loop/planner/out/` | Read Write Edit Grep Glob |
| critic | `critic-run` | `/srv/loop/critic/brief/modules.md` | `/srv/loop/critic/out/` | Read Write |
| solver | `solver-run` → `solver-claude` | `/srv/loop/brief/<phase>.md` | `src/` と `tests/`（柵の場所） | Read Write Edit Grep Glob。Bash は禁止 |

ブリーフは呼び出しのたびに上書きする。置き場に残るのは、役と位相ごとの最後の1回分になる。

### サブエージェント

`/etc/loop/<役>.env` の `LOOP_SUBAGENT_MODEL` にモデルを書くと、そのモデルで動く助手を `--agents` で渡し、`Agent` ツールを足す。空なら渡さない。

| 助手 | 渡す役 | ツール | 任せる仕事 |
|---|---|---|---|
| `scout` | planner、solver | Read Grep Glob | 既存のコードと参照を探し、パスと署名と短い抜粋だけを返す |
| `editor` | solver | Read Write Edit Grep Glob | 中身の決まった書き換え。名前の変更、コードの移動、書き方を指定した定型 |

- 探索で読んだファイルの中身は助手の文脈に入る。役の文脈には答えだけが戻る
- 許すのは上の名前だけ。親のモデルを継ぐ組み込みの助手は呼べない
- 助手は役と同じ uid で動く。書ける場所は役と変わらない
- critic には渡さない

```mermaid
flowchart LR
  runner["runner"]
  planner["planner"]
  critic["critic"]
  solver["solver"]
  runner -->|"plan.md と out/ に写した今の設計"| planner
  planner -->|"basic/ または modules/ と ac/"| runner
  runner -->|"modules.md"| critic
  critic -->|"FINDINGS.json"| runner
  runner -->|"test_write.md / stub.md / impl.md"| solver
  solver -->|"src/ tests/ への書き込み"| runner
```

## planner

| 場面 | 関数 | ブリーフの中身 | 書く場所 |
|---|---|---|---|
| `basic draft` | `brief_basic` | 仕様書、`src/` を読めること、`environment_facts(short=True)`、人のコメント | `basic/` |
| `modules draft` | `brief_modules` | 仕様書、基本設計、`src/` を読めること、`environment_facts(short=True)`、書き直してよいモジュール（差し戻しのとき）、コードの置き方、契約と受け入れ条件の書式、検査の一覧、人のコメント | `modules/`、`ac/` |

ランナーは呼ぶ前に out/ を空にし、今の設計をそこへ写す。プランナーはそれを直接直す。書く場所の外のファイル、拡張子の違うファイル、シンボリックリンクは、ランナーが読まずに消す（`prune_planner_out`）。

どちらの場面も `draft_with_retry` で包む。検査（RUNNER_SPEC §6）に落ちると、消したものと検査の判定をブリーフの末尾に足して呼び直す。呼び直しは最大 `LIMITS["revisions"]` 回。

## critic

`modules critique` が1回だけ呼ぶ。`modules draft` の後にも続けて流れる。

| 関数 | ブリーフの中身 |
|---|---|
| `brief_critique` | 仕様書、基本設計、すべての詳細設計、役の説明、見るもの（Coverage、Reach、Boundaries）、答えの形 |

受け入れ条件、コード、テストは渡さない。

出力は `FINDINGS.json` の1つ。ファイルが無いか読めなければ、ランナーは批評が起きなかったものとして止まる。指摘の `title` と `evidence` は日本語で書かせる。人が読むからだ。設計の名前は、元の言語のまま引かせる。

指摘はプランナーに戻さない。ランナーは `plan/CRITIQUE.json` に設計のハッシュと一緒に書き、写しを `human/in/CRITIQUE.json` に置く。人は写しを `loop findings` で読む。書き直させたいことは、`loop reject` のコメントでプランナーに伝える。

### environment_facts()

planner の両方の場面に入る。中身は次のとおり。

- 言語、ランタイム、テストランナーの版、画面の有無
- ランナーが流すテストのコマンド
- 根の直下と、柵のコードのディレクトリの中のファイル一覧
- 根にある環境のファイルの名前
- 既存のコードの公開宣言の署名（`existing_contracts`）。C# は引数と戻り値の型を外す。本体は渡さない
- 既存のテストのファイルとテストの名前（`existing_tests_text`）
- テストが何に届くかを決めるファイル。C# はランナーが書く Tests.csproj の全文
- TypeScript なら `index.html` の全文

既存のコードが大きいプロジェクトでは、署名の一覧がブリーフの大半を占める。プランナーは本体を `src/` から読める。

## solver

`run_step` が、1モジュールにつき次の順で呼ぶ。

1. TEST_WRITE（`brief_test_write`）: 共通の部分、受け入れ条件、書いてよいファイル、差し替える既存のテストファイル。goal は渡さない。テストがコンパイルできなければ、コンパイラの出力を足して最大 `LIMITS["test_writes"]` 回まで呼び直す
2. STUB: ランナーが契約からスタブを書く。書けないときだけ `brief_stub` で solver に頼む。渡すのは署名と書いてよいファイルだけ
3. IMPL（`brief_impl`）: 共通の部分、goal、凍結したテストの全文、書いてよいファイルの今の中身、このモジュールの前のコードとの差分、直前の失敗、書いてよいファイル。緑になるか試行を使い切るまで、試行の数だけ呼ぶ

TEST_WRITE と IMPL のブリーフは、同じ `solver_material` で始まる。中身は `module_context` が組む背景（基本設計の概要、コードの置き方、自分の詳細設計、依存先の振る舞い）と、依存先の契約と、自分の署名だ。同じモジュールの位相をまたいで同じなので、後の呼び出しはそこをプロンプトキャッシュから読む。IMPL では、試行ごとに変わる今の中身と直前の失敗を後ろに置く。

solver は `plan/`、`docs/`、`ac/` を読めない。設計と受け入れ条件はブリーフに書かれた分しか届かない。

IMPL のブリーフは、書いてよいファイルのうち作業ツリーにあるものの中身を、試行のたびに読み直して載せる（`current_files_section`）。合計が `IMPL_FILE_CHARS` を超える分は名前だけを挙げ、solver が自分で Read する。Claude Code の Edit と Write は、同じ呼び出しで Read していないファイルを拒むが、1行だけの Read でも通る。ブリーフはそれを solver に伝える。ほかの位相では、既存のファイルの中身はブリーフに入らない。

STUB は既存のメソッドの本体をスタブに差し替えるので、今の中身だけでは元の分岐が見えない。solver はそれを知らずに本体を一から書き、`else if` を1つ足せば済むところで分岐をまるごと差し替え、既存のものと同じ static のヘルパーを足していた。そこで、書いてよいファイルのうち最後のコミットにあったものは、今の中身との差分も載せる（`original_code_section`）。`-` の行が元のコードで、solver にはそれを土台に最小の変更をし、既存のヘルパーを呼ぶよう伝える。差分の合計も `IMPL_FILE_CHARS` を超えない分だけを載せる。

## 呼び出し回数

| 役 | 回数 |
|---|---|
| planner | `basic draft` と `modules draft` のたびに 1〜(`revisions` + 1) 回 |
| critic | `modules critique` のたびに 1 回 |
| solver | モジュールごとに、TEST_WRITE 1〜`test_writes` 回、STUB 0〜1 回、IMPL 1〜(試行の数) 回 |

## 消費の記録

`run_agent` は呼び出しごとに `USAGE` を台帳（`plan/ledger.jsonl`）に書く。中身は `claude -p --output-format json` が返す `usage` の全体、モデルごとの内訳（`model_usage`）、`cost_usd`、`duration_ms`、`turns`、モデル。

画面とログには、同じ内容を1行で出す。

```text
[USAGE] who=solver phase=IMPL model=claude-sonnet-5 in=110 out=20 read=100 write=6 sec=4 usd=0.01
```

| 項目 | 中身 |
|---|---|
| `model` | 使ったモデル。サブエージェントのモデルも並ぶ |
| `in` | 入力、キャッシュ読み取り、キャッシュ書き込みの和 |
| `out` | 出力 |
| `read` | キャッシュ読み取り（`cache_read_input_tokens`） |
| `write` | キャッシュ書き込み（`cache_creation_input_tokens`） |

`modelUsage` がモデルごとのトークンの内訳を持つときは、4つの量をその和から出す。サブエージェントの分も入る。内訳が無いときは `usage` から出す。

種類ごとの読み方は次のとおり。

| 種類 | 何の量か |
|---|---|
| キャッシュ書き込み | 新しく渡した中身。ブリーフと、エージェントが読んだファイルの大きさ |
| キャッシュ読み取り | ターンの数 × そのターンまでの文脈の大きさ。Claude Code 自身のシステムプロンプトとツールの定義も毎ターン入る |
| 入力 | キャッシュに載らなかった端数 |
| 出力 | 思考と、書いたファイル |

ダッシュボードの「トークンの種類ごとの消費」は、この4種類を回ごとに積み上げて描く。

## 計測（2026/09/27、取り込んだ Unity のプロジェクトの回）

C# で10ステップを全部緑にした回の、台帳とブリーフの実測。この回の critic は2つのモードで計画を読み、planner は計画を批評に合わせて改訂していた。

### ブリーフの大きさ

| ブリーフ | 字数 | 最大の節 |
|---|---|---|
| critic の coverage | 16.4万 | 既存の宣言 11.2万（68%）、計画 4.2万 |
| critic の trace | 16.5万 | 既存の宣言 11.2万（68%）、計画 4.2万 |
| planner の refine | 4.7万 | 計画 3.9万 |

### 役ごとの消費

台帳全体の値。この台帳には bootstrap の3回分が入っている。

| 役 | 呼び出し | ターン | 入力の合計 | うちキャッシュ読み取り | 出力 | USD |
|---|---|---|---|---|---|---|
| planner（Opus） | 9 | 49 | 350万 | 81% | 19万 | 9.73 |
| critic（Sonnet） | 12 | 24 | 264万 | 56% | 18万 | 6.75 |
| solver（Sonnet） | 21 | 94 | 298万 | 89% | 4万 | 2.26 |

### 読み取れること

- Claude Code 自身の固定費は1ターン約1.7万トークン。ブリーフが約8千の solver の呼び出しでも、2ターンで約5万になる
- critic は毎回2ターンで終わるが、1回約10万のキャッシュ書き込みを払う。大半は既存の宣言の一覧
- planner の bootstrap は約11万の文脈を抱えて7〜12ターン回り、1回で47万〜65万を読む
- solver の消費の63%は、既存のファイルを書き換える3ステップの IMPL だった。10〜23ターン回っている

## 更新履歴

- 2026/09/28: 役どうしの受け渡しと、2026/09/27 の回の計測を追加
- 2026/09/28: IMPL のブリーフに載せるファイルの今の中身を追加
- 2026/09/28: critic と solver のブリーフの共通の先頭を追加
- 2026/09/28: 既存のテストの名前と差し替えを追加
- 2026/09/30: IMPL のブリーフに載せるこのステップの前のコードとの差分を追加
- 2026/09/30: solver のツールに Grep と Glob を追加
- 2026/10/14: planner、critic、solver の受け渡しを3段（上流、中段、下流）の設計に変更
- 2026/10/17: サブエージェントと、消費の記録のモデルごとの内訳を追加
