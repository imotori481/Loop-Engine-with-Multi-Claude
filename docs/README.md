# ドキュメント

Sleep Walking Alternator（SWA）の設計・運用ドキュメントをまとめています。

初めて読む場合は、次の順番を推奨します。

1. [ARCHITECTURE.md](ARCHITECTURE.md) — **この機械は何でできているか**（役、段、輪、繰り返す失敗の形）
2. [BOOTSTRAP.md](BOOTSTRAP.md) — 設計思想と設計仕様書の書き方
3. [RUNNER_SPEC.md](RUNNER_SPEC.md) — ランナーの仕様（段、関門、設計の検査）
4. [LOCAL_SOLVER.md](LOCAL_SOLVER.md) — ローカルソルバーの導入と運用
5. [HANDOFF.md](HANDOFF.md) — 現在地、未完了項目、再開時の注意

普段の操作で使うコマンドは [COMMANDS.md](COMMANDS.md) にまとめてあります。

既存のリポジトリを新しいプロジェクトとして取り込み、切り替えて走らせる流れは [PROJECT_SWITCH.md](PROJECT_SWITCH.md) にまとめてあります。

役どうしの受け渡しとトークン消費の読み方は [AGENT_IO.md](AGENT_IO.md) にまとめてあります。

環境別の手順は、それぞれ [provision/README.md](../provision/README.md) と [host/README.md](../host/README.md) を参照してください。
