# Feature Map

selfim の実装機能カタログ。README(docs/)の各機能に対応する実装位置と検証状況を記す。

## 機能一覧(map)

| # | 機能 | 実装 | 検証 | README対応 |
|---|---|---|---|---|
| F1 | SIMデータベース(D.3準拠) | `src/selfim/schemas.py` (SimRecord系) / `sim_manager.py` | TP-1(16テスト) | docs/reference.md「SIMManager」 |
| F2 | 2段階記録(予測→成果) | `sim_manager.py` record_predicted/record_outcome | TP-1 | 同上・docs/explanation.md「予測を評価前に」 |
| F3 | 根原因マージ+aliases | `sim_manager.py` merge_by_exact_name | TP-1(3件マージ・連鎖alias含む) | 同上 |
| F4 | モジュール分解と隔離適用 | `module_registry.py` | TP-2 | docs/reference.md「ModuleRegistry」 |
| F5 | 受け入れゲート | `validation_gate.py` | TP-2 | docs/reference.md「ValidationGate」 |
| F6 | ルーブリックLLM-judge評価 | `evaluator.py` / `models.py` (JudgeModel) | TP-4 | docs/reference.md「Evaluator」 |
| F7 | 失敗クラスタリング | `evaluator.py` cluster_failures | TP-4 | docs/explanation.md「単発の失敗を」 |
| F8 | 改善候補提案(SIM参照) | `proposer.py` / `models.py` (build_proposer_prompt) | TP-5 | docs/reference.md「Proposer」 |
| F9 | スクリーニング+選択 | `selector.py` | TP-5 | docs/reference.md「Selector」 |
| F10 | 4段階改善ループ | `main_loop.py` | TP-6(E2E 3テスト) | docs/tutorial.md |
| F11 | タスクスイート読込/分割 | `task_loader.py` | TP-4/TP-6経由 | docs/how-to-swap-tasks.md |
| F12 | プロバイダ抽象化(mock/bedrock) | `models.py` / `config_loader.py` | TP-3(mock経路) | docs/how-to-swap-tasks.md「Bedrockで動かす」 |
| F13 | タスクエージェント実行+エビデンス | `agent_runner.py` | TP-3 | docs/reference.md「CLI」 |
| F14 | CLI実行 | `scripts/run_loop.py` | G4実デモ(legal/hr両スイート) | docs/tutorial.md手順 |
| F15 | サンプルスイート2種 | `data/tasks/legal_judgment/` `hr_evaluation/` | G4実デモ | docs/how-to-swap-tasks.md |

## 検証状況(2026-11-22時点)

- `uv run pytest tests/` → **58 passed**
- G4実デモ:
  - `demo-legal-002` (legal_judgment, 2ラウンドk=2): evolve 56.00 → 100.00, held-out 56.00 → 100.00, P1採択(fixed=2/2)
  - `demo-hr-002` (hr_evaluation, 1ラウンドk=2): evolve 50.00 → 80.00, held-out 50.00 → 80.00, P1採択
- 成果物配置: `output/runs/<run_id>/{sim.json, report.json, splits.json, evaluations/, harness/}`

## 既知の制限(README Explanation「残した限界」と対応)

- bedrock経路はスモークテスト未実施(APIキーなし)。構造化出力の組み立てはテスト済み
- ジャッジの決定性: mockは決定的、bedrockはLLM判断に依存
- SIM全文参照方式のコンテキスト上限(docs/explanation.md 参照)
