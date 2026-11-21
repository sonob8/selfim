# テスト計画(G3)

`tests/` は pytest。すべてmockプロバイダで実行し、APIキーを不要とする。実装順に単位コミットする。

## TP-1: schemas + sim_manager
- SIMのJSON roundtrip が論文A D.3 の全フィールド(round, sample, accepted, backfilled, what, files, predicted, fixed, missed, passed, total, domain)を保持する
- 新規根原因で record_predicted → problems に新エントリ(id=P1, opened_round 記録)
- 既存根原因ID引用で record_predicted → 既存エントリに attempt 追記(履歴保持)
- record_outcome が直前の pending attempt に fixed/missed/passed/total を埋める
- merge_by_exact_name: 同名エントリをマージし aliases に記録、試行は全て保持
- render_for_prompt が全エントリの根原因名と what を含む(truncationなし)

## TP-2: module_registry + validation_gate
- snapshot がシードをrunワークスペースへ複製、シードは不変
- apply_candidate が対象モジュール1つだけ変更した候補ディレクトリを返す
- diff が unified diff を返す
- ゲート: 構文エラーの候補を拒否 / 非退行(候補スコア<ベース)を拒否 / diff行数超過を拒否 / 実行エビデンス欠落(tool呼出0件)を拒否 / 全条件充足で合格

## TP-3: config + models + agent_runner
- settings.yaml + .env の読込(既定値・上書き)
- build_model("mock") が役割別クライアントを返す / bedrock未認証では明確なエラー
- AgentRunner がモジュールからタスク出力を生成し、tool呼出をCallLogに記録する
- system prompt の指示(思考ステップ列挙等)が出力内容に反映される(mockの改善感受性)

## TP-4: evaluator
- ルーブリック重み付き採点(0-100)
- 不足ステップが missing_points に列挙される
- attribution(harness/model)の導出
- cluster_failures: 同一署名の失敗を集約、count==1 は除外

## TP-5: proposer + selector
- k件の提案を生成、各提案は根原因引用 or 新規命名(排他)、target_module は1つ、predicted 非空
- プロンプトにSIM全文とD.2の8原則が含まれる
- スクリーニング: タスク固有ヒント(タスクID直書き)を拒否 / 複数モジュール横断を拒否 / 行数予算超過を拒否 / SIM上失敗済み方向の無根拠再提案を拒否
- 選択: スコア向上候補を採択 / 全候補が退行なら不採択(None)を返す

## TP-6: main_loop E2E (mock)
- 2ラウンド k=2 でスコアが向上し、output/runs/<id>/ に sim.json(D.3形式)・評価ログ・採択済みハーネスが生成される
- 不採択ラウンドで sim.json がバイト等価に不変
- held-out before/after がレポートに記録され、ループ中の候補が held-out に触らない

## カバレッジ目標
上記ユニット+E2Eで src/selfim の主要分岐を網羅(目安80%+)。カバレッジ数値自体はゲームせず、ゲート規則・SIM不変性・スキーマ適合の検証を優先する。
