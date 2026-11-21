# Reference: コンポーネントAPI

パッケージは `src/selfim/` にあります。すべての公開クラスは `schemas.py` のPydanticモデルを入出力に使います。ここに書くのは現時点で実装が確定している公開APIです。

## SIMManager (`sim_manager.py`)

SIMデータベースの読み書きを担います。1 runにつき1つのJSONファイル(`output/runs/<run_id>/sim.json`)を管理します。

- `SIMManager(path: Path)` — runディレクトリの `sim.json` を束縛します。存在しなければ空のSIM(`next_id=1`、`problems=[]`)を初期化します
- `.load() -> SimRecord` — ファイルからSIMを読み込みます
- `.save(record: SimRecord) -> None` — SIMを書き込みます
- `.render_for_prompt(record: SimRecord) -> str` — 全エントリをプロンプト用テキストに整形します。検索もtruncationも行いません(全エントリ全文)
- `.record_predicted(record, proposal, round_no, sample_id, domain="") -> SimRecord` — 採択候補の予測を評価の前に記録します。`problem_key` の参照があればそのエントリに追記し、なければ新規エントリを作ります
- `.record_outcome(record, fixed, missed, passed, total) -> SimRecord` — 評価後に `fixed`/`missed`/`passed`/`total` を埋めます。直前の `record_predicted` の対象にのみ作用します
- `.merge_by_exact_name(record) -> SimRecord` — 根原因名(`name`)の完全一致するエントリをマージし、`aliases` に記録します(aliasの向き先も最終IDへ書き換えます)。試行履歴は失われません
- `.has_pending_outcome -> bool` — 予測記録済みで成果未記録の試行があるかを返します
- `.find_problem_by_key(record, key) -> SimProblem | None` — ID・根原因名・aliasのいずれかでエントリを検索します(クラスメソッド)

不採択ラウンドでは `record_predicted` / `record_outcome` を呼びません。その結果、SIMは変わりません。

## Evaluator (`evaluator.py`)

ルーブリック採点と失敗クラスタリングを担います。

- `Evaluator(model)` — ジャッジ用モデルを束縛します
- `.evaluate(output_text, task, rubric) -> EvaluationResult` — 1rolloutの出力をジャッジします。`EvaluationResult` は `score`(0–100)、`reason`、`missing_points`、`attribution`(`harness` / `model`)を持ちます
- `.run_suite(runner, rubric, tasks, rollouts, round_no, label="evolve") -> SuiteFeedback` — タスクセット全体を評価します(タスク × rollouts 回の実行と採点)。スコア集計とタスクごとの `EvaluationResult` を返します
- `.cluster_failures(feedback) -> list[FailureCluster]` — 複数rolloutの失敗を不足点の署名でクラスタリングします。`FailureCluster` は `signature`、`task_ids`、`count`、`root_cause_hypothesis` を持ちます。単発の失敗(`count < 2`)と `attribution == "model"` の失敗はクラスタに入りません

## Proposer (`proposer.py`)

改善候補の生成を担います。

- `Proposer(model, sim_manager)` — 提案用モデルとSIMを束縛します
- `.propose(harness_sources, clusters, sim_record, task_ids, k) -> list[Proposal]` — 失敗クラスタとSIM全文から `k` 件の `Proposal` を生成します。クラスタがなければ空リストを返します
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

- `Selector(model, diff_line_budget)` — 選択モデルと行数予算を束縛します
- `.screen(proposals, sim_record, task_ids) -> ScreenResult` — 実行前に候補を落とします。却下規則: タスク固有ヒント(タスクID直書き)の検出、行数予算超過、SIM履歴で失敗済み方向の無根拠な再提案。複数モジュール横断編集は `Proposal` スキーマが生成自体を拒否します。スクリーニング落ち候補は評価されません
- `.select(base_score, evaluated) -> SelectionResult` — ゲート通過候補の中から1件を採択するか、全不採択を返します。判断材料はスコア向上・複雑度・退行リスク(モデル助言 + スコアの決定的最終判定)

## MainLoop (`main_loop.py`)

ループ全体の制御を担います。

- `MainLoop(evaluator, proposer, selector, sim_manager, module_registry, gate, task_model, rubric, run_dir, rollouts_per_task, suite_name="")` — 全コンポーネントを束縛します
- `.run(tasks, splits, n_rounds, k_candidates) -> RunReport` — ラウンドを `n_rounds` 回繰り返します。各ラウンドは「評価 → 提案 → スクリーニング(manifest記録) → 適用 → 検証ゲート → 再評価 → 採択判断 → SIM更新」の順です
- held-outセットの評価は `.run()` 内の開始時(before)と終了時(after)にのみ実行され、ループ内の候補・予測・クラスタには渡りません

## ModuleRegistry (`module_registry.py`)

ハーネスのモジュール分解と候補の隔離適用を担います。

- `ModuleRegistry(seed_dir, workspace_dir)` — シード(`src/selfim/harness/`)とrunワークスペースを束縛します
- `.snapshot() -> None` — シードをrunワークスペースへ複製します。シードは以降書き換わりません(ワークスペースが既にあればスキップ = 継続実行)
- `.module_names() -> list[ModuleName]` — 4モジュール(`system_prompt` / `skill` / `tool` / `verify`)の名前を返します
- `.read_module(name, base_dir=None) -> str` — モジュール内容を返します(既定は現行ワークスペース)
- `.apply_candidate(proposal, candidate_id) -> Path` — 候補を `candidates/<candidate_id>/` 配下に隔離適用し、候補ハーネスのパスを返します。現行ハーネスには直接書き込みません
- `.promote(candidate_dir) -> None` — 採択候補を現行ワークスペースへ昇格します
- `.diff(name, candidate_dir) -> str` — 現行ハーネスとのunified diffを返します
- `.diff_line_count(name, candidate_dir) -> int` — diffの追加・削除行数を返します(予算判定用)

## ValidationGate (`validation_gate.py`)

採択前の受け入れチェックを担います。4条件すべてを満たさない候補は拒否されます。

- `ValidationGate(diff_line_budget, token_budget=None)` — 予算を束縛します
- `.check(proposal, diff_lines, evidence, candidate_score, base_score, candidate_tokens=0) -> GateResult` — 次を検査します:
	1. Pythonモジュール(tool/verify)の `compile()` が成功すること
	2. 実行エビデンスがあること(system_prompt/skill はプロンプト組込、tool/verify は import成功 + tool呼出1件以上)
	3. evolveスコアがベースライン以上であること(非退行)
	4. diff行数が `diff_line_budget` 以内であること
	5. `token_budget` 設定時は実行トークンが上限以内であること

`GateResult` は `passed: bool` と `reasons: list[str]`(拒否理由)を持ちます。

## models (`models.py`)

プロバイダファクトリです。

- `build_model(role, provider, model_id="", region="us-west-2") -> Model` — 役割(`task` / `judge` / `proposer` / `selector`)とプロバイダ(`bedrock` / `mock`)からモデルを組み立てます。`model_id`/`region` はbedrock用(.env の `BEDROCK_MODEL_ID`/`BEDROCK_REGION` から渡します)
- mock実装 — 決定的な応答を返すテスト用モデルです。ハーネス内容に敏感で(system promptの指示マーカーが出力構成を変える)、APIキーを必要としません
- `build_proposer_prompt(...)` — Proposer用プロンプト(system promptにSIM全文とD.2の8原則を埋め込む)を組み立てる純関数です

## AgentRunner (`agent_runner.py`)

ハーネスディレクトリからタスクエージェントを組み立て実行します。

- `AgentRunner(harness_dir, model)` — ハーネスディレクトリ(system_prompt.txt / skill.md は必須)とタスクモデルを束縛します
- `.run_task(task) -> AgentOutput` — 1タスクを実行します。`AgentOutput` は `text` / `call_log` / `prompt` / `tokens` を持ちます
- `.execution_evidence(output, module) -> ExecutionEvidence` — ValidationGate に渡す実行エビデンスを構築します(system_prompt/skill は内容のプロンプト組込確認、tool/verify は import成功 + CallLog の呼出数)
- `CallLog` — モジュール実行の呼出記録。`.record(module_file, function_name)` と `.count_for(module_file)` を持ちます

## task_loader (`task_loader.py`)

タスクスイートの読み込みと分割を行います。

- `load_suite(suite_dir) -> (list[TaskSpec], Rubric)` — tasks.yaml と rubric.yaml を読み込みます。task_id重複は拒否します
- `load_splits(suite_dir, tasks, run_seed) -> (evolve, held_out)` — splits.yaml があればそれを優先し、なければ run_seed で決定的に分割します(奇数の場合はevolve側が偶数を取ります、論文A Appendix B.2)

## CLI (`scripts/run_loop.py`)

```sh
uv run python scripts/run_loop.py --suite <suite> --provider <mock|bedrock> [--rounds N] [--candidates K] [--run-id ID]
```

- `--suite`(必須)— `data/tasks/` 配下のスイート名
- `--provider` — 既定値は `.env` の `SELFIM_MODEL_PROVIDER`(未設定なら `mock`)
- `--rounds` — 改善ラウンド数。既定値は `config/settings.yaml` の `n_rounds`
- `--candidates` — 1ラウンドの候補数。既定値は `k_candidates`
- `--run-id` — run識別子(未指定は `<suite>-<provider>-<タイムスタンプ>`)
