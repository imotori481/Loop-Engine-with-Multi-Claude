# LOCAL_SOLVER.md — ローカルソルバーの導入と、1本目の実験

ソルバーの段にローカルモデル（Qwen3.5-9B / nonthinking）を足すための手順書。
既定のソルバーは Claude Code（`claude`）で、ローカルモデルは計画の `solver_tiers` で
名前を挙げたときだけ使う。**この文書だけで最後まで進められる**ように書いてある。

設計の根拠は RUNNER_SPEC §4-4-1、判断の経緯は HANDOFF §5。ここは操作だけ。

---

## 0. この文書の作り方について

**費用は「動かすこと」ではなく「曖昧な失敗が生む往復」で発生する。**
だから以下は、失敗が起きない前提では書いていない。失敗が起きたとき、

- どこで落ちたかを**スクリプト自身が名乗る**（`smoke-local` は番号付きで PASS/FAIL を出す）
- 対処が**その場に書いてある**（下の失敗表）
- それでも分からないときに**貼るものが1つに決まっている**（`smoke-local` 末尾の `--- paste this ---` ブロック）

という形にしてある。人に聞く前に、まず `smoke-local` を通すこと。

---

## 1. 手順

### 1-1. llama.cpp を入れる（**ビルドが必要**）

```bash
/usr/lib/wsl/lib/nvidia-smi     # WSL 内から GPU が見えるか。PATH には無い
```

**リリースバイナリでは済まない。** llama.cpp は Linux 向けの CUDA バイナリを
配布しておらず（Windows 向けのみ）、Linux 用の Vulkan ビルドは WSL では
`llvmpipe`（ソフトウェアラスタライザ）しか見つけられない ── つまり GPU で動いて
いるように見えて CPU で回る。2026-09-01 にこの箱で実測。

```bash
# CUDA ツールチェーン（ドライバは Windows 側。ここでは入れない）
curl -sSLO https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt-get update
VER=$(apt-cache search '^cuda-nvcc-[0-9]' | awk '{print $1}' | sed 's/cuda-nvcc-//' | sort -V | tail -1)
sudo apt-get install -y "cuda-nvcc-$VER" "cuda-cudart-dev-$VER" "libcublas-dev-$VER"

git clone --depth 1 https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=75 -DLLAMA_CURL=OFF
cmake --build build -j"$(nproc)" --target llama-server
sudo install -m 755 build/bin/llama-server /usr/local/bin/
```

`75` は Turing（RTX 2060）。**アーキテクチャを1つに絞ること**と
**`llama-server` だけを作ること**で、ビルド時間の大半が消える。
それでも6コアで30分前後かかる（`ggml-cuda` の flash-attention テンプレートが大半）。

### 1-1b. この箱の実測値（2026-09-01）

| | |
|---|---|
| GPU | RTX 2060 / **VRAM 6144 MiB** / driver 591.59 / `/dev/dxg` あり |
| CPU / RAM | 6コア / 7.8 GiB（WSL VM の割当） |
| 選んだ量子化 | `Qwen3.5-9B-IQ4_XS.gguf` **4.81 GB** |
| ctx | **8192**（`--cache-type-k/v q8_0`）。max_output 4096 |

VRAM 6 GB に対し重みが 4.81 GB なので、残りで KV キャッシュと計算バッファを賄う。
`Q4_K_M`（5.29 GB）は full offload では入らない。入らなければ `Q3_K_M`（4.35 GB）へ。
**ctx を上げるより量子化を落とすほうが先** ── ブリーフ＋対象ファイルは 2〜3k トークンで、
8192 は既に余裕がある。

### 1-2. 重みを置く

```bash
sudo install -o root -g llm -m 0440 <model>.gguf /srv/loop/models/model.gguf
```

4bit 量子化で約 6GB。**0440 root:llm** なのは、solver アカウントに自分を動かしている
モデルのファイルを読ませる理由が無いため（取り上げたものを返さない）。

### 1-3. 配る

```bash
cd /tmp && sudo bash /opt/loop-engine/provision/70-local-solver.sh
```

`/opt/loop-engine` は箱の中のこのリポジトリのクローン（`provision/README.md` §2-6）。

uid `llm` の作成、`/srv/loop/models` の権限、3本のスクリプトの設置まで。
**sudoers は変更しない** — `solver-run` が持つ Runas(solver) の1つの許可で足りる。

### 1-4. サーバを起動する

```bash
sudo systemctl enable --now loop-llm          # systemd がある場合
# 無い場合:
sudo -u llm setsid nohup /srv/loop/bin/llm-serve >/srv/loop/models/serve.out 2>&1 &
```

VM が生きている間だけ生きる。**`wsl --shutdown` 厳禁**（README 3-2）。

### 1-5. 配管を確かめる ← ここまで利用枠を使わない

```bash
sudo -u solver /srv/loop/bin/smoke-local
```

7項目。全部 PASS なら「配管は問題ではない」ことが確定する。
7番だけ FAIL する場合、それは**配管ではなくモデルの話**で、スクリプトがそう言う。

### 1-6. 1本回す

```bash
# run 5 のプランをそのまま使う。題材を変えない ── 変えると
# 「モデルが弱い」のか「その題材が難しい」のかが分離できない
sudo -u runner python3 /srv/loop/runner/loop.py reset S1     # 必要なら
```

`plan/tasks.json` の最上位に足す:

```json
"solver_tiers": ["local", "claude"],
"policy":       {"retry": "resample"},
"limits":       {"attempts": 8}
```

`solver_tiers` は必ず書く ── **既定は `["claude"]`** で、書かなければローカルモデルは
呼ばれない（RUNNER_SPEC §7 末尾）。`local` が試行を使い切ったステップだけを `claude` が
引き継ぐ。ローカルだけで測るなら `["local"]` にする。

`resample` と温度 0.7 は**対で意味を持つ**。温度 0 で `resample` すると8回とも同じ
答えを引く。逆に `repair` にするなら温度は下げてよい。

```bash
sudo -u runner python3 -u /srv/loop/runner/loop.py run --all 2>&1 \
  | sudo -u runner tee -a /srv/loop/logs/<名前>.log
```

走行ログは `/srv/loop/logs/` に書く（`provision/README.md` §2-10）。

---

## 2. 失敗表

### `solver-local` の終了コード

| コード | 意味 | 対処 |
|---|---|---|
| 0 | 動いた（**中身が駄目でも 0**） | 下記「0 で返るのに何も起きない」参照 |
| 2 | 引数かブリーフが読めない | `solver-run` の呼び方。通常は起きない |
| 5 | ブリーフに `# Files you may create or modify` が無い | `loop.py` のブリーフ書式が変わった。`brief_impl()` と突き合わせる |
| 6 | プロンプトがコンテキストに入らない | `llm-serve` の `--ctx-size` と `LOOP_LLM_CTX` を上げる。上げられないならステップを分割 |
| 7 | 推論サーバに届かない／拒否された | `pgrep -a -u llm llama-server`。落ちていれば 1-4 |
| 124 | 制限時間内に返らない | `timeouts.solver` を上げる。CPU 推論なら数倍必要 |

**非ゼロは全部エスカレーション＝プランナー呼び出し＝Claude の消費**になる。
だから「モデルが下手」は**絶対に非ゼロにしない**設計にしてある（下記）。

### 症状から引く

**0 で返るのに何も書かれない / VERIFY が毎回同じ失敗**
モデルが JSON を守れなかったか、許可外のパスを返した。`solver-local` が
`wrote nothing` / `refused [...]` と標準出力に書いている。台帳ではなく `/srv/loop/logs/` の走行ログを見る。
これは**設計どおり**の挙動 ── モデルの失敗は試行を1回消費するだけで、プランナーには届かない。
何度も続くならステップが大きすぎる（次項）。

**ステップが大きすぎる**
`files_write` が2ファイル以上、あるいは実装が100行を超える見込みなら、9B には重い。
対処は `max_attempts` を増やすことではなく、**bootstrap をやり直してステップを細かくする**
（1ステップ＝1関数、`files_write` は1つ、テストは3本まで）。ただし bootstrap は
プランナー＝Claude を使うので、**1本目の結果を見てから**判断する。

**thinking が消えていない**（`smoke-local` の 6 が FAIL、または `<think>` 警告）
順に: (a) `llm-serve` に `--jinja` があるか ── これが無いとテンプレートが走らず
`enable_thinking` は黙って無視される。(b) その GGUF のテンプレートが
`enable_thinking` を読むか。**プロンプトで「考えるな」と書くのは対処ではない**
（要請は破られる。テンプレート側で経路を消す）。

**server too old**（不明なフィールドで 400、`/props` が `n_ctx` を返さない）
`chat_template_kwargs` と `response_format: json_schema` に対応した llama.cpp が要る。
新しいリリースバイナリに入れ替える。

**遅い**（`smoke-local` の 4 が FAIL、tok/s が1桁）
GPU に載っていない。`nvidia-smi` と `--n-gpu-layers`。載せられないなら設計は変わらないが、
`timeouts.solver` を大きく取り直し、1本の実時間の見積もりを作り直す。

**上の段に落ちた瞬間に固まる**
`60-egress.sh` を loopback のみに締めた状態で `solver_tiers` に `claude` か `codex` が残っている。
締めるのは**ローカル単独運用（`["local"]`）に切り替えてから**。

---

## 3. 1本目に観測すること

run 5（Codex）が比較対象。**run 6 の実測値を入れてある**（2026-09-01）。

| | run 5 (Codex) | run 6 (Qwen3.5-9B local) |
|---|---|---|
| 無介入の緑 | 11/11 | **11/11** |
| ステップあたり試行数 | ほぼ1 | **1（全ステップ attempt=1）** |
| **エスカレーション回数** | 1 | **0** |
| codex 段に落ちたステップ数 | ─ | **0** |
| 実時間 | 19分37秒 | **15分12秒** |
| 最終スイート | 48 passed | **48 passed**（skipped 0 / regressions []） |
| 消費した課金アカウント | Codex | **なし** |

**エスカレーション回数が最重要**。それが Claude の消費そのもので、
「ソルバーを安くしたら費用がプランナー側へ移った」かどうかはこの数字だけで分かる。

台帳から取れる:

```bash
sudo -u runner grep -c ESCALATED /srv/loop/project/plan/ledger.jsonl
sudo -u runner grep '"phase":"VERIFY"' /srv/loop/project/plan/ledger.jsonl \
  | python3 -c 'import sys,json,collections
c=collections.Counter()
for l in sys.stdin: r=json.loads(l); c[r.get("backend")]+=1
print(c)'
```

### 自動では測れない1つ

**通ったコードを1回だけ目で見る。** 見るのは「テストを通すためのハードコード」
（`if n == 3: return 7` の類）。失敗出力を返す × 弱いモデル × 試行回数を増やす、の
3つが揃うと主要な失敗モードになる。FREEZE はテストの改変を止めるが、これは止めない。

出ていたら、次はホールドアウト（§4）を入れる。出ていなければ入れない。

### RED_GATE が落ちる場合の読み方

R1（収集数の不一致）／R4（スタブで通る）／R5（赤が本物でない）が目立つなら、
**問題は IMPL ではなく TEST_WRITE** ── 9B が受け入れ条件からテストを書けていない。
テストは凍結されて基準になるので、ここが弱いのは実装が弱いより悪い。
位相ごとにバックエンドを変える仕組みは**まだ無い**。必要になったら作る（それが証拠）。

---

## 4. まだ決めていないこと

**ホールドアウト。** プランナーがテストを可視群と秘匿群に分け、ブリーフには可視群だけを
描画し、RED_GATE と VERIFY は全部を走らせる。判断を含まない純粋な機構。
ただし **IMPL 中の `tests/` は 0444 でソルバーから読める**（`loop.py` の `set_writable`）
ので、いまのままでは要請であって剥奪ではない。本物にするには `tests/` を runner のみに
する必要がある。`claude` と `local` はもともと pytest を回せないので、自己検証を失う
代償は `codex` 段にだけかかる。**1本目の目視で必要性を決める。**

**位相ごとのバックエンド。** 上記の RED_GATE の読み方を参照。

**egress の締め直し。** ローカル単独運用に切り替えたら `solver` は外向き通信が
一切不要になる。この設計で初めてそこまで締められる（`lo` は既に許可されている）。

---

## 更新履歴

- 2026/09/26: 既定のソルバー段を `claude` とし、`solver_tiers` を必ず書く手順に変更。配布と走行ログの場所を箱のクローン構成に合わせて変更
