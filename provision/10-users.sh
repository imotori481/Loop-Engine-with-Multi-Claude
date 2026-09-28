#!/usr/bin/env bash
# ユーザーとグループ。冪等。
#
# maint   : ディストロの既定ユーザー（uid 1000）。sudo あり。保守とプロビジョニング
#           専用で、ループには含まれない。名前は ADMIN_USER で渡す。
# runner  : すべてを所有し、ループを回す。sudo なし
# solver  : src/（凍結前は tests/ も）だけを書く。sudo なし、ssh なし
# planner : 計画の提案だけを書く。sudo なし、ssh なし。solver と分けるのは、
#           受け入れ条件を決めるアカウントと満たすアカウントを、プロンプトの
#           違いではなく uid の違いにするため（BOOTSTRAP 1-1）。資格情報を
#           分けるのも同じ理由による。
# critic  : ブリーフを読み、指摘を書く。sudo なし、ssh なし。planner への2つ目の
#           プロンプトではなく4つ目の uid にするのは、価値がすべて「見えない
#           こと」から来るからだ。判定する計画を読める critic は「全基準を満たして
#           いる」と答える。真だが役に立たない。tests/ を読める critic は
#           「テストは通る」と答える。どちらも実測した。だから分離は指示ではなく
#           ファイルシステムの事実にする。
#
# ループのアカウントは、互いのホームも保守ユーザーのホームも読めてはならない。
# だからリポジトリはどのホームの下でもなく /srv/loop に置く。
set -euo pipefail

ADMIN_USER="${ADMIN_USER:-maint}"

id -u "$ADMIN_USER" >/dev/null 2>&1 || {
  echo "FATAL: 保守ユーザー '$ADMIN_USER' がいない。" >&2
  echo "       保守ユーザーはディストロの既定ユーザーだ。名前が違うなら ADMIN_USER で渡す。" >&2
  exit 1
}

id -u runner >/dev/null 2>&1 || useradd -m -u 1001 -s /bin/bash runner
id -u solver >/dev/null 2>&1 || useradd -m -u 1002 -s /bin/bash solver
id -u planner >/dev/null 2>&1 || useradd -m -u 1003 -s /bin/bash planner
id -u critic  >/dev/null 2>&1 || useradd -m -u 1004 -s /bin/bash critic

# solver に特定のディレクトリへの書き込みを許すためのグループ。
getent group solverw >/dev/null || groupadd solverw
usermod -aG solverw solver
usermod -aG solverw runner   # runner がグループの権限で brief/ に書けるように

# planner は solverw を共有せず、自分のグループを持つ。共有すると planner が
# tests/ と src/ に届いてしまう。それは BOOTSTRAP 1-1 の分離そのものに反する。
# 受け入れ条件を決める者は、それを満たすコードに触れてはならない。
getent group plannerw >/dev/null || groupadd plannerw
usermod -aG plannerw planner
usermod -aG plannerw runner

# critic のグループ。あえて plannerw でも solverw でもない。どちらかに入れば、
# critic は planner/out か brief/ に届く。判定する作業が書かれる2つの場所だ。
# critic はブリーフを渡され、指摘を書く。ほかに触る必要のあるものは無い。
getent group criticw >/dev/null || groupadd criticw
usermod -aG criticw critic
usermod -aG criticw runner

# 人間のグループ。保守ユーザーを入れるのは、人が sudo なしで
# /srv/loop/human/in にファイルを置けるようにするため。runner を入れるのは、
# 置かれたものを読めるようにするため。solver も planner も入れない。そこが
# 要点だ。プロジェクトを始める要件と、後のエスカレーションへの答えは、人だけが
# 言ってよい2つのことだ。
#
# sudo を持つ保守ユーザーにとって、これは権限の追加ではない。Web の画面が
# 自分の uid で書き込みを引き継いだときに、同じディレクトリがそのまま使える
# ようにしてある。
getent group humanw >/dev/null || groupadd humanw
usermod -aG humanw runner
usermod -aG humanw "$ADMIN_USER"

# ループのアカウントはどれも sudo できない。前提にせず確かめる。紛れ込んだ
# sudoers の drop-in が1つあるだけで、環境を凍結するという主張全体が黙って崩れる。
for u in runner solver planner critic; do
  if id -nG "$u" | tr ' ' '\n' | grep -qxE 'sudo|admin'; then
    echo "FATAL: $u is in a sudo-capable group" >&2
    exit 1
  fi
done

# ホームは所有者だけのもの。保守ユーザーのホームには人間の ssh 鍵とエージェント
# CLI の資格情報が置かれるので、solver に読ませてはならない。
chmod 700 /home/runner /home/solver /home/planner /home/critic
if [ -d "/home/$ADMIN_USER" ]; then chmod 700 "/home/$ADMIN_USER"; fi

echo "10-users: ok"
