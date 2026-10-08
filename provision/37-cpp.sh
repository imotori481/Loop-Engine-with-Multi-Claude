#!/usr/bin/env bash
# C++ のツールチェーン。C++ の計画をこの箱で走らせるためにある。
#
# g++、make、CMake、GoogleTest を Ubuntu の archive から入れる。どれも /usr の下に
# 入り、root のものなので、solver はツールチェーンに触れられない。ネットワークに
# 出るのは、足りないパッケージがあるときのこのスクリプトだけだ。
#
# 箱で確かめるのは標準の C++17 のロジックだけ。DXライブラリや Windows の API は
# Linux に無いので入れない。それを include するファイルは、ランナーがビルドから
# 外す（runner/loop.py の cpp_sources）。
#
# CMakeLists.txt はプロジェクトの外（/srv/loop/cpp/build）に置き、ランナーが柵の
# 場所から書く。取り込んだプロジェクトの根に置くと、そのプロジェクトのビルドの
# 設定とぶつかる。
set -euo pipefail
cd "$(dirname "$0")"

TOOLS=/srv/loop/cpp
BUILD="$TOOLS/build"
PACKAGES=(g++ make cmake libgtest-dev)

install -d -o root -g root -m 755 /srv/loop/bin
install -o root -g root -m 755 bin/smoke-cpp /srv/loop/bin/smoke-cpp

# ---- パッケージ ------------------------------------------------------------
# libgtest-dev は、ビルド済みの libgtest.a と libgtest_main.a と、CMake の
# find_package(GTest) が読む設定を持つ。
missing=()
for package in "${PACKAGES[@]}"; do
  dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
done
if [ "${#missing[@]}" -gt 0 ]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi
g++ --version | head -n 1
cmake --version | head -n 1

# ランナーが CMakeLists.txt を書き、ビルドするところ。中身はコンパイルしたコードと
# テストなので、runner だけが入れる。planner と critic に見えてはならない
# （BOOTSTRAP 1-1）。solver はコマンドを走らせないので、ここを要らない。
install -d -o root -g root -m 755 "$TOOLS"
install -d -o runner -g runner -m 700 "$BUILD"

# ---- 検査 ------------------------------------------------------------------
fail=0
if ! sudo -u runner -H /srv/loop/bin/smoke-cpp; then
  echo "FAIL: C++ のテストをビルドして走らせられない"; fail=1
fi
for who in solver planner critic; do
  id -u "$who" >/dev/null 2>&1 || continue
  if sudo -u "$who" ls "$BUILD" >/dev/null 2>&1; then
    echo "FAIL: $who should NOT be able to: ls $BUILD"; fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "37-cpp: TOOLCHAIN BROKEN" >&2
  exit 1
fi
echo "37-cpp: ok"
