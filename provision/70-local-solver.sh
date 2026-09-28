#!/usr/bin/env bash
# ローカルのソルバーのバックエンド。モデルサーバ用のアカウント、スクリプト、
# 重みの置き場を用意する。冪等。
#
#     cd /tmp && sudo bash /opt/loop-engine/provision/70-local-solver.sh
#
# 60-egress.sh と同じ理由で、provision.sh からは流さない。ローカルモデルを
# 使うと決めたときにだけ正しい。
#
# sudoers の変更は要らず、何も変えない。solver-run はすでに Runas(solver) の
# 許可を1つ持ち、このバックエンドを同じ uid で exec する。ランナーは持って
# いなかったものを何も得ない。コマンドではなくバックエンドの名前を渡す形に、
# ファイルを1つ増やすだけの価値があったのは、この性質のためだ。
set -euo pipefail
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# あえて別のアカウントにする。モデルサーバは solver でも runner でもない。
# solver はループバックで届くが、再起動も、重みを読むことも、起動のしかたを
# 変えることもできない。ここのほかの uid と同じ考え方で、柵はアカウントであり、
# 誰が何を呼ぶかの取り決めではない。
if ! id -u llm >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/llm --shell /usr/sbin/nologin llm
  chmod 700 /home/llm
  echo "created uid llm"
fi

# 重みとサーバのログ。root の所有で、llm が読む。ほかに読む必要のある者はいない。
install -d -o root -g root -m 755 /srv/loop/models
chown root:llm /srv/loop/models
chmod 2750 /srv/loop/models

install -o root -g root -m 755 bin/llm-serve    /srv/loop/bin/llm-serve
install -o root -g root -m 755 bin/solver-local /srv/loop/bin/solver-local
install -o root -g root -m 755 bin/smoke-local  /srv/loop/bin/smoke-local

echo
echo "置いたもの:"
echo "  /srv/loop/bin/llm-serve      （llm として動かす）"
echo "  /srv/loop/bin/solver-local   （計画が local を挙げたとき、solver-run が起動する）"
echo "  /srv/loop/bin/smoke-local    （solver として流す）"

# --------------------------------------------------------------------------
# このスクリプトが確かめずには行わない唯一の手順。
# --------------------------------------------------------------------------
if ! command -v llama-server >/dev/null 2>&1; then
  cat <<'EOF'

llama-server が PATH に無い。ビルドする必要がある。好みの問題ではない。
llama.cpp は Linux 向けの CUDA 版を配っていない（Windows 向けだけ）。Linux の
Vulkan 版は、WSL では llvmpipe しか見つけない（2026-09-01 にこの箱で測った）。
つまり、GPU で動いているように見えて CPU で動く。

  sudo -u <you> bash ~/build-llama.sh      # LOCAL_SOLVER.md 1-1 を参照

  要点: wsl-ubuntu のリポジトリから cuda-keyring を入れ、cuda-nvcc、
  cuda-cudart-dev、libcublas-dev を入れてから、

    cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
          -DCMAKE_CUDA_ARCHITECTURES=75 -DLLAMA_CURL=OFF
    cmake --build build -j"$(nproc)" --target llama-server

  75 は Turing（RTX 2060）。アーキテクチャを1つに絞るとビルド時間の大半が減り、
  ターゲットを llama-server だけにすると残りの大半が減る。

  GPU があるかは /usr/lib/wsl/lib/nvidia-smi で分かる（PATH には無い）。GPU が
  無ければ、9B のモデルを6コアの CPU で推論することになる。毎秒数トークン、
  1回の試行に数百秒かかる。計画を走らせる前に LOCAL_SOLVER.md の時間切れの注意を
  読む。設計は変わらないが、数字が変わる。
EOF
fi

if [ ! -r /srv/loop/models/model.gguf ] && [ -z "${LOOP_LLM_WEIGHTS:-}" ]; then
  cat <<'EOF'

重みがまだ無い。.gguf を /srv/loop/models/model.gguf に置く:

    install -o root -g llm -m 0440 <ダウンロードしたもの>.gguf /srv/loop/models/model.gguf

0440 root:llm は意図したものだ。solver のアカウントには、自分に答えを返している
モデルを読む理由が無い。読ませれば、設計が取り上げたものを返すことになる。
EOF
fi

# --------------------------------------------------------------------------
# サーバを動かし続ける
# --------------------------------------------------------------------------
if [ -d /run/systemd/system ]; then
  cat > /etc/systemd/system/loop-llm.service <<'EOF'
[Unit]
Description=loop local inference server
After=network.target

[Service]
User=llm
ExecStart=/srv/loop/bin/llm-serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  echo
  echo "systemd がある。次で起動する:"
  echo "    systemctl enable --now loop-llm"
  echo "    systemctl status loop-llm --no-pager"
else
  cat <<'EOF'

このディストロでは systemd が動いていないので、有効にするユニットが無い。
サーバを切り離して起動し、そのまま置いておく:

    sudo -u llm setsid nohup /srv/loop/bin/llm-serve >/srv/loop/models/serve.out 2>&1 &

サーバは VM が生きているあいだ動く。VM は keepalive タスクが生きているあいだ動く。
だから README 3-2 の決まりがここでも効く。`wsl --shutdown` は使わない。
EOF
fi

cat <<'EOF'

次:
    sudo -u solver /srv/loop/bin/smoke-local

そのあと、plan/tasks.json に:
    "solver_tiers": ["local", "claude"],
    "policy": {"retry": "resample"},
    "limits": {"attempts": 8}

solver_tiers は必須だ。既定は ["claude"] で、書かなければローカルモデルは一度も
呼ばれない。

60-egress.sh についての注意: solver_tiers に "claude" か "codex" が残っているなら、
solver は外の API のホストに届く必要がある。外向きをループバックだけに絞ると、
予備の段が壊れる。しかもエラーではなく時間切れとして壊れる。絞るのは、段の
並びが ["local"] だけになってからにする。
EOF
