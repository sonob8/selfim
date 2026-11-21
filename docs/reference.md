# Reference: コンポーネントAPI

パッケージは `src/selfim/` にあります。すべての公開クラスは `schemas.py` のPydanticモデルを入出力に使います。ここに書くのは現時点で実装が確定している公開APIです。

## SIMManager (`sim_manager.py`)

SIMデータベースの読み書きを担います。1 runにつき1つのJSONファイル(`output/runs/<run_id>/sim.json`)を管理します。

- `SIMManager(path: Path)` — runディレクトリの `sim.json` を束縛します。存在しなければ空のSIM(`next_id=1`、`problems=[]`)を初期化します
- `.load() -> SimRecord` — ファイルからSIMを読み込みます
- `.save(record: SimRecord) -> None` — SIMを書き込みます
- `.render_for_prompt(record: SimRecord) -> str` — 全エントリをプロンプト用テキストに整形します。検索もtruncationも行いません(全エントリ全文)
- `.record_predicted(record, proposal, round_no, sample_id) -> SimRecord` — 採択候補の予測を評価の前に記録します。`problem_id` の参照があればそのエントリに追記し、なければ新規エントリを作ります
- `.record_outcome(record, round_no, fixed, missed, passed, total) -> SimRecord` — 評価後に `fixed`/`missed`/`passed`/`total` を埋めます。直前の `record_predicted` の対象にのみ作用します
- `.merge_by_exact_name(record) -> SimRecord` — 根原因名(`name`)の完全一致するエントリをマージし、`aliases` に記録します。試行履歴は失われません

不採択ラウンドでは `record_predicted` / `record_outcome` を呼びません。その結果、SIMは変わりません。

## Evaluator (`evaluator.py`)

ルーブリック採点と失敗クラスタリングを担います。

- `Evaluator(model, rubric_loader)` — ジャッジ用モデルとルーブリックを読み込みます
- `.evaluate(agent_output, task, rubric) -> EvaluationResult` — 1タスク1rolloutをジャッジします。`EvaluationResult` は `score`(0–100)、`reason`、`missing_points`、`attribution`(`harness` / `model`)を持ちます
- `.run_suite(harness_dir, tasks, rollouts) -> SuiteFeedback` — evolveセット全体を評価します。スコア集計とタスクごとの `EvaluationResult` を返します
- `.cluster_failures(feedback) -> list[FailureCluster]` — 複数rolloutの失敗をエラー署名でクラスタリングします。`FailureCluster` は `signature`、`task_ids`、`count`、`root_cause_hypothesis` を持ちます。単発の失敗(`count == 1`)はクラスタに入りません

## Proposer (`proposer.py`)

改善候補の生成を担います。

- `Proposer(model, sim_manager)` — 提案用モデルとSIMを束縛します
- `.propose(harness_sources, failure_clusters, history, sim_record, k) -> list[Proposal]` — `k` 件の `Proposal` を生成します
- `Proposal` のフィールド:
	- `cites_problem: str | None` — 既存根原因のID。新規の場合は `None`
	- `new_problem_name: str | None` — 新規根原因の名前。既存引用の場合は `None`
	- `target_module: ModuleName` — 編集対象モジュール(1候補につき1モジュール)
	- `what: str` — 編集内容とその理由
	- `new_content: str` — モジュール全体の新しい内容
	- `predicted: list[str]` — 修正される見込みのタスクID
	- `rationale: str` — SIMの過去試行と突き合わせた判断

system promptにはSIM全文と、論文A D.2の8原則が埋め込まれます。

## Selector (`selector.py`)

実行前スクリーニングと評価後選択を担います。

- `Selector(model, budgets)` — 行数予算・トークン予算を束縛します
- `.screen(proposals, sim_record, budgets) -> list[Proposal]` — 実行前に候補を落とします。却下理由: タスク固有ヒントの検出、複数モジュールへの横断編集、行数予算超過、SIM履歴で失敗済み方向の無根拠な再提案。スクリーニング落ち候補は評価されません
- `.select(base_score, evaluated_candidates) -> SelectionResult` — 評価後の候補から1件を採択するか、全不採択を返します。判断材料はスコア向上・複雑度・ドメイン特化度・退行リスクです

## MainLoop (`main_loop.py`)

ループ全体の制御を担います。

- `MainLoop(evaluator, proposer, selector, sim_manager, module_registry, validation_gate, config)` — 全コンポーネントを束縛します
- `.run(tasks, splits, n_rounds, k_candidates) -> RunReport` — ラウンドを `n_rounds` 回繰り返します。各ラウンドは「評価 → 提案 → スクリーニング → 適用 → 検証ゲート → 再評価 → 採択判断 → SIM更新」の順です
- `.evaluate_held_out(harness_dir, tasks) -> SuiteFeedback` — held-outセットを評価します。ループ内からは呼ばれず、開始時と終了時のみ実行します

## ModuleRegistry (`module_registry.py`)

ハーネスのモジュール分解と候補の隔離適用を担います。

- `ModuleRegistry(seed_dir, workspace_dir)` — シード(`src/selfim/harness/`)とrunワークスペースを束縛します
- `.snapshot() -> None` — シードをrunワークスペースへ複製します。シードは以降書き換わりません
- `.module_names() -> list[ModuleName]` — 4モジュール(`system_prompt` / `skill` / `tool` / `verify`)の名前を返します
- `.read_module(name) -> str` — 現ワークスペースのモジュール内容を返します
- `.apply_candidate(proposal) -> Path` — 候補を `candidates/<candidate_id>/` 配下に隔離適用し、候補ハーネスのパスを返します。現行ハーネスには直接書き込みません
- `.diff(name, candidate_dir) -> str` — 現行ハーネスとのunified diffを返します

## ValidationGate (`validation_gate.py`)

採択前の受け入れチェックを担います。4条件すべてを満たさない候補は拒否されます。

- `ValidationGate(diff_line_budget, token_budget)` — 予算を束縛します
- `.check(candidate_dir, proposal, candidate_score, base_score) -> GateResult` — 次を検査します:
	1. Pythonモジュールの `compile()` とimportが成功すること
	2. 候補ハーネスが実タスクを1回以上実行し、編集されたコードパスに到達すること
	3. evolveスコアがベースライン以上であること(非退行)
	4. diff行数が `diff_line_budget` 以内であること

`GateResult` は `passed: bool` と `reasons: list[str]`(拒否理由)を持ちます。

## models (`models.py`)

プロバイダファクトリです。

- `build_model(role: str, provider: str) -> Model` — 役割(`task` / `judge` / `proposer` / `selector`)とプロバイダ(`bedrock` / `mock`)からモデルを組み立てます。設定は `.env` と `config/settings.yaml` から読みます
- `MockModel` — 決定的な応答を返すテスト用モデルです。APIキーを必要としません

## CLI (`scripts/run_loop.py`)

```sh
uv run python scripts/run_loop.py --suite <suite> --provider <mock|bedrock> [--rounds N] [--candidates K]
```

- `--suite`(必須)— `data/tasks/` 配下のスイート名
- `--provider` — 既定値は `.env` の `SELFIM_MODEL_PROVIDER`(未設定なら `mock`)
- `--rounds` — 改善ラウンド数。既定値は `config/settings.yaml` の `n_rounds`
- `--candidates` — 1ラウンドの候補数。既定値は `k_candidates`
