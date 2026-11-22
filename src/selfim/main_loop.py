"""MainLoop — 改善ループ全体の制御(論文A Eq.(1)/(3) × 論文B 4段階)。

1ラウンドの流れ:
  Stage 1  Ft = Evaluator(現行ハーネス, evolveセット)   — 評価 + 失敗クラスタリング
  Stage 2  Ct = Proposer(Ht, Ft, Dt, SIM全文)           — 候補生成(根原因引用/新規命名)
  Screening  Selector.screen(Ct)                         — 実行前スクリーニング
           manifest に予測を記録(D.1: 評価前に保存)
  Stage 3  候補適用 → ValidationGate → evolve再評価      — 検証
  Stage 4  Selector.select → 採択 or 全不採択(Ht+1 = Ht) — 配備判断
  SIM更新   採択候補のみ record_predicted + record_outcome(2段階、設計判断 D-2)

安全上の制約:
- held-out評価はループ外(run 内の before/after)でのみ実行する
- 不採択ラウンドではSIMに触れない(U(M,∅)=M)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from selfim.agent_runner import AgentRunner, TaskRunner
from selfim.evaluator import Evaluator
from selfim.module_registry import ModuleRegistry, read_all_modules
from selfim.proposer import Proposer
from selfim.schemas import (
    CandidateEvaluation,
    ExecutionEvidence,
    Proposal,
    RoundRecord,
    RunReport,
    ScreenResult,
    SuiteFeedback,
    TaskSpec,
)
from selfim.selector import Selector
from selfim.sim_manager import SIMManager
from selfim.validation_gate import ValidationGate

logger = logging.getLogger(__name__)

# rollout を「成功」とみなすスコア閾値(SIMの passed/total 計算用)。
# ルーブリックの中間帯を成功としないため 60 を採用した。
_MIN_SUCCESS_SCORE = 60.0


class MainLoop:
    """全コンポーネントを束縛し、改善ループを実行する。

    Attributes:
        evaluator / proposer / selector / sim_manager / registry / gate: 各コンポーネント。
        task_model: タスクエージェントモデル(候補実行用に AgentRunner を都度組む)。
        rubric: ユーザー提供ルーブリック。
        run_dir: run成果物のディレクトリ(output/runs/<run_id>/)。
        rollouts_per_task: 1タスクあたりのrollout数。
    """

    def __init__(
        self,
        evaluator: Evaluator,
        proposer: Proposer,
        selector: Selector,
        sim_manager: SIMManager,
        module_registry: ModuleRegistry,
        gate: ValidationGate,
        task_model: Any,
        rubric: Any,
        run_dir: Path,
        rollouts_per_task: int,
        suite_name: str = "",
        runner_factory: Callable[[Path], TaskRunner] | None = None,
    ) -> None:
        """コンポーネント群を束縛する。

        Args:
            evaluator / proposer / selector / sim_manager / module_registry / gate: 各コンポーネント。
            task_model: build_model("task", ...) の返り値。
            rubric: ユーザー提供ルーブリック。
            run_dir: run成果物ディレクトリ。
            rollouts_per_task: 1タスクあたりのrollout数。
            suite_name: スイート名(SIMのdomainフィールドとレポートに記録される)。
            runner_factory: 実行器ファクトリ(ハーネスディレクトリ → TaskRunner)。
                None なら local 実行系(AgentRunner)をその場で組む。
                docker実行系では DockerAgentRunner を組むファクトリを渡す
                ([DECISION 2026-11-22] 実行系の選択をこのシームに集約)。
        """
        self.evaluator = evaluator
        self.proposer = proposer
        self.selector = selector
        self.sim_manager = sim_manager
        self.registry = module_registry
        self.gate = gate
        self.task_model = task_model
        self.rubric = rubric
        self.run_dir = run_dir
        self.rollouts_per_task = rollouts_per_task
        self.suite_name = suite_name
        self._runner_factory = runner_factory
        self._evaluations_dir = run_dir / "evaluations"
        self._evaluations_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        tasks: list[TaskSpec],
        splits: tuple[list[TaskSpec], list[TaskSpec]],
        n_rounds: int | None = None,
        k_candidates: int | None = None,
    ) -> RunReport:
        """改善ループを実行し、RunReport を返す。

        Args:
            tasks: スイートの全タスク(ID記録用)。
            splits: (evolveタスク, held-outタスク)。
            n_rounds: ラウンド数(None は設定値)。論文A §4.1 は N=10。
            k_candidates: 1ラウンドの候補数(None は設定値)。論文A §4.1 は k=5。

        Returns:
            RunReport(ラウンド記録とheld-out before/afterを含む)。
        """
        evolve, held_out = splits
        n_rounds = n_rounds if n_rounds is not None else 3
        k_candidates = k_candidates if k_candidates is not None else 3

        # [FIX 2026-11-22] Proposer/Selector には evolve セットのIDのみ渡す。
        # 以前は全タスクID(tasks)を渡していたため、mock Proposer が held-out ID を
        # predicted に含め、SIM の fixed/missed が誤記録される実害があった
        # (3点照合 2026-11-22 で demo-legal-002 の sim.json fixed=[] として検出)。
        # 論文A Appendix B.2: evolve側のスコアと軌跡のみが最適化に使われる。
        evolve_task_ids = [t.task_id for t in evolve]

        # snapshot は初回のみ。同一runディレクトリへの再実行は「継続」とみなし、
        # 現行ワークスペース(前回採択済みハーネス)から最適化を続ける
        # (CRSI の継続意味論に整合)。
        if not (self.registry.workspace_dir / "modules").exists():
            self.registry.snapshot()
        report = RunReport(
            run_id=self.run_dir.name, suite=self.suite_name, provider="", n_rounds=n_rounds,
            k_candidates=k_candidates,
        )
        # ラウンド0: シードハーネスのベースライン評価
        base_feedback = self._evaluate_harness(self.registry.workspace_dir, evolve, 0)
        base_score = base_feedback.score
        base_per_task = self._per_task_scores(base_feedback)
        report.evolve_before = base_score

        # held-out before(ループ外で実施、候補には渡さない)
        held_before = self._evaluate_harness(
            self.registry.workspace_dir, held_out, 0, label="held_out"
        )
        report.held_out_before = held_before.score

        sim_record = self.sim_manager.load()

        for round_no in range(1, n_rounds + 1):
            logger.info("=== round %d (base %.2f) ===", round_no, base_score)
            current_feedback = self._evaluate_harness(
                self.registry.workspace_dir, evolve, round_no
            )
            record, sim_record, base_per_task, base_score = self._run_round(
                round_no=round_no,
                base_score=base_score,
                base_per_task=base_per_task,
                sim_record=sim_record,
                current_feedback=current_feedback,
                evolve=evolve,
                task_ids=evolve_task_ids,
                k_candidates=k_candidates,
                domain=self.suite_name or "suite",
            )
            report.rounds.append(record)
            # SIMの永続化(不採択ラウンドでも現在状態を確定させる)
            self.sim_manager.save(sim_record)
            self._dump_round_feedback(round_no, current_feedback, record)

        report.evolve_final = base_score
        held_after = self._evaluate_harness(
            self.registry.workspace_dir, held_out, n_rounds, label="held_out"
        )
        report.held_out_after = held_after.score
        logger.info(
            "loop done: evolve %.2f -> %.2f, held-out %.2f -> %.2f",
            report.evolve_before, report.evolve_final,
            report.held_out_before, report.held_out_after,
        )
        return report

    # ------------------------------------------------------------------
    # 1ラウンド
    # ------------------------------------------------------------------

    def _run_round(
        self,
        round_no: int,
        base_score: float,
        base_per_task: dict[str, float],
        sim_record: Any,
        current_feedback: SuiteFeedback,
        evolve: list[TaskSpec],
        task_ids: list[str],
        k_candidates: int,
        domain: str,
    ) -> tuple[RoundRecord, Any, dict[str, float], float]:
        """1ラウンドを実行する。

        Args:
            round_no: ラウンド番号(1始まり)。
            base_score: 現行ハーネスのevolveスコア。
            base_per_task: 現行ハーネスのタスク別平均スコア。
            sim_record: 現在のSIM。
            current_feedback: 現行ハーネスのラウンド開始時評価(Stage 1)。
            evolve: evolveセット。
            task_ids: 全タスクID。
            k_candidates: 候補数。
            domain: SIMのdomainフィールドに記録するスイート名。

        Returns:
            (RoundRecord, 更新後SIM, 更新後タスク別スコア, 更新後スコア)。
        """
        record = RoundRecord(round_no=round_no, base_score=base_score)
        clusters = self.evaluator.cluster_failures(current_feedback)

        # Stage 2: 提案(ProposerがSIM全文を参照して根原因を引用/命名する)
        sources = self._load_sources(self.registry.workspace_dir)
        proposals = self.proposer.propose(sources, clusters, sim_record, task_ids, k_candidates)

        # 実行前スクリーニング + manifest 記録(予測は評価前に固定する、論文A D.1)
        screen = self.selector.screen(proposals, sim_record, task_ids)
        record.screened_out = [d.reason for d in screen.rejected]
        self._dump_manifest(round_no, screen, proposals)

        evaluated: list[CandidateEvaluation] = []
        # [FIX 2026-11-22] 採択IDに対応する候補ディレクトリ・評価結果をIDで直接引く。
        # 以前は「スコア最大のゲート通過候補」を別変数で保持していたため、
        # セレクタが別候補を採択すると SIM記録(採択候補)と昇格コード(最高候補)が
        # 異なる候補を指す不整合が起き得た(最終diff-review 2026-11-22 で指摘)。
        by_candidate_id: dict[str, tuple[Path, SuiteFeedback]] = {}

        # Stage 3: 候補適用 → 検証ゲート → evolve再評価
        for i, proposal in enumerate(screen.accepted):
            candidate_id = f"s{i + 1}"
            candidate_dir = self.registry.apply_candidate(proposal, candidate_id)
            # [DECISION 2026-11-22] docker実行系はコンテナを保持するため、
            # 候補ごとの評価ブロック全体を try/finally で閉じ、確実に close する。
            candidate_runner = self._runner_for(candidate_dir)
            try:
                cand_feedback = self._evaluate(
                    candidate_runner, evolve, round_no, label=f"candidate_{candidate_id}"
                )
                diff_lines = self.registry.diff_line_count(proposal.target_module, candidate_dir)
                # 実行エビデンスはサンプルタスクの実際の実行から得る(設計判断 D-3)
                sample_out = candidate_runner.run_task(evolve[0]) if evolve else None
                evidence = (
                    candidate_runner.execution_evidence(sample_out, proposal.target_module)
                    if sample_out is not None
                    else ExecutionEvidence()  # 空evolveはデフォルト(全False)でゲート拒否側に倒す
                )
                gate = self.gate.check(
                    proposal=proposal,
                    diff_lines=diff_lines,
                    evidence=evidence,
                    candidate_score=cand_feedback.score,
                    base_score=base_score,
                    candidate_tokens=sample_out.tokens if sample_out is not None else 0,
                    # [DECISION 2026-11-22] docker実行系のビルド・スモーク成否を
                    # 実行器から取得してゲートへ渡す(local実行系は属性を持たない
                    # ため True 既定。getattr で両実行系を透過的に扱う)。
                    build_ok=getattr(candidate_runner, "build_ok", True),
                    smoke_ok=getattr(candidate_runner, "smoke_ok", True),
                )
            finally:
                candidate_runner.close()
            ce = CandidateEvaluation(
                candidate_id=candidate_id,
                proposal=proposal,
                gate=gate,
                score=cand_feedback.score if gate.passed else 0.0,
                diff_lines=diff_lines,
            )
            evaluated.append(ce)
            if gate.passed:
                by_candidate_id[candidate_id] = (candidate_dir, cand_feedback)
            logger.info(
                "candidate %s: score=%.2f gate=%s (%s)",
                candidate_id, cand_feedback.score, gate.passed, gate.reasons,
            )

        record.evaluated = evaluated

        # Stage 4: 選択と配備。不採択なら Ht+1 = Ht、SIMは不変。
        # [FIX 2026-11-22] セレクタにもSIM全文を渡す(論文A §3.2: 提案者とセレクタは
        # 同じ記録を参照する)。以前は sim_text="" を渡しており、実LLMセレクタが
        # 過去の失敗試行を知らないまま採択判断していた(新規コンテキストレビューで指摘)。
        selection = self.selector.select(
            base_score, evaluated, sim_text=self.sim_manager.render_for_prompt(sim_record)
        )
        if selection.accepted_candidate_id is None:
            logger.info("round %d: kept Ht (%s)", round_no, selection.reason)
            return record, sim_record, base_per_task, base_score

        chosen_ce = next(c for c in evaluated if c.candidate_id == selection.accepted_candidate_id)
        # 採択IDがゲート通過候補にない場合(セレクタ判断の無効性)は不採択扱い
        if chosen_ce.score <= base_score or chosen_ce.candidate_id not in by_candidate_id:
            return record, sim_record, base_per_task, base_score

        chosen_dir, chosen_feedback = by_candidate_id[chosen_ce.candidate_id]

        # SIM更新(2段階、設計判断 D-2): 採択候補のみ記録する
        sim_record = self.sim_manager.record_predicted(
            sim_record, chosen_ce.proposal, round_no, chosen_ce.candidate_id, domain=domain
        )
        fixed, missed, passed, total = self._outcome(
            chosen_ce.proposal, base_per_task, chosen_feedback
        )
        sim_record = self.sim_manager.record_outcome(
            sim_record, fixed=fixed, missed=missed, passed=passed, total=total
        )

        # 配備: 採択候補を現行ワークスペースへ昇格
        self.registry.promote(chosen_dir)
        record.accepted_candidate_id = chosen_ce.candidate_id
        record.accepted_score = chosen_ce.score
        record.sim_updated = True
        return record, sim_record, self._per_task_scores(chosen_feedback), chosen_ce.score

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _runner_for(self, harness_dir: Path) -> TaskRunner:
        """指定ハーネスディレクトリに束縛した実行器を作る。

        [DECISION 2026-11-22] 実行系(local/docker)の選択をこのシームに集約する。
        runner_factory が渡されていればそれを使い、なければ従来どおり
        in-process の AgentRunner を組む(既存テスト・local実行は無変更で動作)。
        呼び出し側は使用後に必ず close() すること(docker実行系はコンテナ停止を伴う)。
        """
        if self._runner_factory is not None:
            return self._runner_factory(harness_dir)
        return AgentRunner(harness_dir=harness_dir, model=self.task_model)

    def _evaluate_harness(
        self,
        harness_dir: Path,
        tasks: list[TaskSpec],
        round_no: int,
        label: str = "evolve",
    ) -> SuiteFeedback:
        """実行器を組立てて評価し、使用後に必ず後始末する。

        docker実行系では実行器がコンテナを保持するため、評価ごとに
        生成・破棄を確実に行う(try/finally で close を保証)。
        """
        runner = self._runner_for(harness_dir)
        try:
            return self._evaluate(runner, tasks, round_no, label=label)
        finally:
            runner.close()

    def _evaluate(
        self, runner: TaskRunner, tasks: list[TaskSpec], round_no: int, label: str = "evolve"
    ) -> SuiteFeedback:
        """タスクセットを評価する薄いラッパー。"""
        return self.evaluator.run_suite(
            runner=runner, rubric=self.rubric, tasks=tasks,
            rollouts=self.rollouts_per_task, round_no=round_no, label=label,
        )

    @staticmethod
    def _per_task_scores(feedback: SuiteFeedback) -> dict[str, float]:
        """タスクIDごとの平均スコアを返す(fixed/missed 判定用)。"""
        sums: dict[str, list[float]] = {}
        for r in feedback.per_task:
            sums.setdefault(r.task_id, []).append(r.score)
        return {tid: sum(v) / len(v) for tid, v in sums.items()}

    def _outcome(
        self,
        proposal: Proposal,
        base_per_task: dict[str, float],
        candidate_feedback: SuiteFeedback,
    ) -> tuple[list[str], list[str], int, int]:
        """候補の評価結果から fixed/missed/passed/total を算出する。

        fixed: 予測タスクのうち、ベースラインよりスコアが向上したもの。
        missed: 予測タスクのうち、向上しなかったもの(外れた予測も記録する、論文A)。

        Args:
            proposal: 採択候補(予測を含む)。
            base_per_task: ベースラインのタスク別平均スコア。
            candidate_feedback: 候補のevolve評価。

        Returns:
            (fixed, missed, passed, total)。
        """
        cand_per_task = self._per_task_scores(candidate_feedback)
        fixed: list[str] = []
        missed: list[str] = []
        for tid in proposal.predicted:
            base = base_per_task.get(tid, 0.0)
            new = cand_per_task.get(tid, 0.0)
            (fixed if new > base else missed).append(tid)
        passed = sum(1 for r in candidate_feedback.per_task if r.score >= _MIN_SUCCESS_SCORE)
        total = len(candidate_feedback.per_task)
        return fixed, missed, passed, total

    def _load_sources(self, harness_dir: Path) -> dict[str, str]:
        """ハーネスモジュールを読み、Proposer/モデル用のソース辞書を作る。

        [DECISION 2026-11-22] 7モジュール(足場を含む)を読み込むよう変更。
        MODULE_PATHS と単一の対応付け(module_registry.read_all_modules)で
        読み込むため、モジュール追加時にここを編集する必要がない。
        """
        return read_all_modules(harness_dir)

    def _dump_manifest(self, round_no: int, screen: ScreenResult, proposals: list[Proposal]) -> None:
        """スクリーニング結果と予測をmanifestとして評価前に書き出す(論文A D.1)。"""
        path = self._evaluations_dir / f"manifest_round_{round_no}.json"
        payload = {
            "proposals": [p.model_dump() for p in proposals],
            "accepted": [p.model_dump() for p in screen.accepted],
            "rejected": [{"reason": d.reason, "what": d.proposal.what} for d in screen.rejected],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _dump_round_feedback(
        self, round_no: int, feedback: SuiteFeedback, record: RoundRecord
    ) -> None:
        """ラウンドの評価結果を書き出す(Evaluatorログ、G4エビデンス用)。"""
        path = self._evaluations_dir / f"round_{round_no}.json"
        payload = {
            "round": round_no,
            "base_score": record.base_score,
            "accepted_candidate_id": record.accepted_candidate_id,
            "per_task": [r.model_dump() for r in feedback.per_task],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
