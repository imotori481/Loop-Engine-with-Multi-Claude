#!/usr/bin/env bash
# .NET のツールチェーン。C# の計画をこの箱で走らせるためにある。
#
# 凍結の考え方は 30-python.sh と 35-node.sh と同じだ。solver はツールチェーンに
# 触れられず、パッケージを足して逃げることもできない。違いは2つある。
#
#   1. パッケージはローカルのフィードから入れる。NuGet のフィードはプロジェクトが
#      restore のたびに読むもので、egress を閉じた後も、このフォルダだけで足りる
#      ようにしておく。ネットワークに出るのは、版を変えたときのこのスクリプトだけ
#   2. csproj はプロジェクトの外（/srv/loop/dotnet/build）に置き、ランナーが柵の
#      場所から書く。プロジェクトの根に置くと、取り込んだ Unity のプロジェクトでは
#      IDE が Unity の生成した csproj と一緒にそれを拾い、Directory.Build.props は
#      Unity の csproj の出力先まで変えてしまう
set -euo pipefail
cd "$(dirname "$0")"

TOOLS=/srv/loop/dotnet
FEED="$TOOLS/feed"
BUILD="$TOOLS/build"

# 版を固定する。範囲で書くと、数か月後の restore が、誰も選ばないうちに関門の
# 意味を変えてしまう。NUnit は 3 系にする。Unity の Test Framework が持つ NUnit は
# 3 系で、4 系は Assert.AreEqual のような古い書き方を ClassicAssert に移した。
# 箱で通ったテストが Unity で通らなくなる。
#
# SDK は2つ入れる。Unity の計画は 8 で、Promete の計画は 10 で走る。Promete 2.1.0 は
# net10.0 だけを持つ。どちらを使うかは、ランナーが build に置く global.json が決める
# （loop.py の dotnet_global_json）。
DOTNET_SDKS="8.0 10.0"
NUNIT_VERSION="3.14.0"
NUNIT_ADAPTER_VERSION="4.6.0"
TEST_SDK_VERSION="17.11.1"
JUNIT_LOGGER_VERSION="4.1.0"
PROMETE_VERSION="2.1.0"

export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1

install -d -o root -g root -m 755 /srv/loop/bin
install -o root -g root -m 755 bin/smoke-dotnet /srv/loop/bin/smoke-dotnet
install -o root -g root -m 755 bin/probe-unity /srv/loop/bin/probe-unity
install -o root -g root -m 755 bin/probe-promete /srv/loop/bin/probe-promete

# ---- SDK -------------------------------------------------------------------
# Ubuntu の archive にある .NET を使う。Microsoft の apt リポジトリを足すと、
# 同じ名前のパッケージが2つの出どころから来て、どちらが入るかが apt の気分になる。
# 2つの SDK は /usr/lib/dotnet に並んで入る。
for sdk in $DOTNET_SDKS; do
  if ! dotnet --list-sdks 2>/dev/null | grep -q "^${sdk}\."; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "dotnet-sdk-$sdk"
  fi
done
dotnet --list-sdks

# ---- ローカルのフィード -----------------------------------------------------
# 版が変わったときだけネットワークに出る。egress を閉じた後にこのスクリプトを
# 流し直しても、版が同じなら何もしない。
WANT="NUnit=$NUNIT_VERSION NUnit3TestAdapter=$NUNIT_ADAPTER_VERSION Microsoft.NET.Test.Sdk=$TEST_SDK_VERSION JunitXml.TestLogger=$JUNIT_LOGGER_VERSION Promete=$PROMETE_VERSION"
install -d -o root -g root -m 755 "$TOOLS"
if [ "$(cat "$FEED/.versions" 2>/dev/null)" != "$WANT" ]; then
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  # テストのパッケージは net8.0 と net10.0 の両方で、Promete は net10.0 で restore する。
  # 推移的な依存は対象ごとに違いうる。両方を拾うには SDK 10 が要る。
  cat > "$WORK/global.json" <<EOF
{"sdk": {"version": "10.0.100", "rollForward": "latestFeature", "allowPrerelease": false}}
EOF
  cat > "$WORK/Fetch.csproj" <<EOF
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFrameworks>net8.0;net10.0</TargetFrameworks>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="NUnit" Version="$NUNIT_VERSION" />
    <PackageReference Include="NUnit3TestAdapter" Version="$NUNIT_ADAPTER_VERSION" />
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="$TEST_SDK_VERSION" />
    <PackageReference Include="JunitXml.TestLogger" Version="$JUNIT_LOGGER_VERSION" />
  </ItemGroup>
  <ItemGroup Condition="'\$(TargetFramework)' == 'net10.0'">
    <PackageReference Include="Promete" Version="$PROMETE_VERSION" />
  </ItemGroup>
</Project>
EOF
  # 推移的な依存もすべて入る。フィードはその .nupkg を平らに並べたフォルダ。
  (cd "$WORK" && dotnet restore "$WORK/Fetch.csproj" --packages "$WORK/packages")
  rm -rf "$FEED.new"
  install -d -m 755 "$FEED.new"
  find "$WORK/packages" -name '*.nupkg' -exec cp {} "$FEED.new/" \;
  echo "$WANT" > "$FEED.new/.versions"
  rm -rf "$FEED"
  mv "$FEED.new" "$FEED"
fi

# ソースはこのフィードだけ。<clear/> が無いと、ユーザーやマシンの設定にある
# nuget.org が混ざり、フィードに無いパッケージをネットワークから黙って拾う。
cat > "$TOOLS/nuget.config" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="loop" value="$FEED" />
  </packageSources>
</configuration>
EOF

# ---- 凍結 ------------------------------------------------------------------
# フィードと設定は root のもの。runner も含めて誰も書けない。
chown -R root:root "$FEED" "$TOOLS/nuget.config"
chmod -R a+rX,go-w "$FEED" "$TOOLS/nuget.config"

# ランナーが csproj を書き、ビルドするところ。中身はコンパイルしたコードと
# テストなので、runner だけが入れる。planner と critic に見えてはならない
# （BOOTSTRAP 1-1）。solver はコマンドを走らせないので、ここを要らない。
install -d -o runner -g runner -m 700 "$BUILD"

# ---- 検査 ------------------------------------------------------------------
fail=0
if ! sudo -u runner -H /srv/loop/bin/smoke-dotnet; then
  echo "FAIL: 凍結したフィードで C# のテストを走らせられない（Unity か Promete の組み合わせ）"; fail=1
fi
for who in solver planner critic; do
  id -u "$who" >/dev/null 2>&1 || continue
  if sudo -u "$who" test -w "$FEED"; then
    echo "FAIL: $who should NOT be able to: test -w $FEED"; fail=1
  fi
  if sudo -u "$who" ls "$BUILD" >/dev/null 2>&1; then
    echo "FAIL: $who should NOT be able to: ls $BUILD"; fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "36-dotnet: TOOLCHAIN BROKEN" >&2
  exit 1
fi
echo "36-dotnet: ok"
