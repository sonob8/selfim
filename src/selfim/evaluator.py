"""Evaluator — ルーブリック採点と失敗クラスタリング(論文B Stage 1)。

責務:
- 1タスク1rolloutのジャッジ結果生成(数値スコア + 理由 + 不足点 + 帰属)
- evolveセット全体の評価(run_suite)とスコア集計
- 繰り返し失敗クラスタの検出(cluster_failures)

論文B §III に従い、単発の失敗(count == 1)はクラスタ化しない。
これにより Proposer は「繰り返し失敗クラスタ」にのみ反応し、
Phantom Guardrails(起きなかった失敗への規則追加)を防ぐ。
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from selfim.agent_runner import AgentRunner
from selfim.models import JudgeModel
from selfim.schemas import (
    Attribution,
    EvaluationResult,
    FailureCluster,
    Rubric,
    SuiteFeedback,
    TaskSpec,
)

logger = logging.getLogger(__name__)

# クラスタリングの最小出現回数。1(単発)は編集対象にしない。
_MIN_CLUSTER_COUNT = 2

# 不足点のキーワード → 根原因仮説テンプレート(クラスタの診断ドラフト)。
# MockJudge の missing_points 語彙に対応し、Proposer のSIM照合に使う。
_ROOT_CAUSE_TEMPLATES: list[tuple[str, str]] = [
    ("思考ステップ欠落", "required reasoning steps are omitted from the output"),
    ("事実の引用", "conclusions lack explicit citation of task facts"),
    ("反対解釈", "counterarguments and fairness considerations are not examined"),
]


class Evaluator:
    """ジャッジモデルを束縛し、評価とクラスタリングを行う。

    Attributes:
        model: JudgeModel 実装(mock または bedrock)。
    """

    def __init__(self, model: JudgeModel) -> None:
        """ジャッジモデルを束縛する。

        Args:
            model: build_model("judge", ...) の返り値。
        """
        self.model = model

    def evaluate(self, output_text: str, task: TaskSpec, rubric: Rubric) -> EvaluationResult:
        """1rolloutの出力をルーブリック採点する。

        Args:
            output_text: タスクエージェントの出力。
            task: 対象タスク。
            rubric: ユーザー提供ルーブリック。

        Returns:
            EvaluationResult(スコア・理由・不足点・帰属)。
        """
        return self.model.judge(task, rubric, output_text)

    def run_suite(
        self,
        runner: AgentRunner,
        rubric: Rubric,
        tasks: list[TaskSpec],
        rollouts: int,
        round_no: int,
        label: str = "evolve",
    ) -> SuiteFeedback:
        """タスクセット全体を評価する(タスク × rollouts 回の実行と採点)。

        Args:
            runner: harness_dir に束縛済みの AgentRunner。
            rubric: ルーブリック。
            tasks: 評価対象タスク。
            rollouts: タスクごとの実行回数。
            round_no: ラウンド番号(記録用)。
            label: "evolve" または "held_out"。

        Returns:
            集合スコアと全評価結果を含む SuiteFeedback。
        """
        per_task: list[EvaluationResult] = []
        for task in tasks:
            for r in range(rollouts):
                out = runner.run_task(task)
                result = self.evaluate(out.text, task, rubric)
                result.rollout = r
                per_task.append(result)
        score = (
            sum(r.score for r in per_task) / len(per_task) if per_task else 0.0
        )
        logger.info(
            "%s evaluation (round %d): score=%.2f over %d rollouts",
            label, round_no, score, len(per_task),
        )
        return SuiteFeedback(
            label=label, round_no=round_no, score=round(score, 2), per_task=per_task
        )

    def cluster_failures(self, feedback: SuiteFeedback) -> list[FailureCluster]:
        """失敗を不足点の署名でクラスタリングする(論文B §III)。

        attribution == "harness" の失敗のみ対象とする(モデル限界は編集対象外)。
        不足点の集合を署名とし、出現回数が _MIN_CLUSTER_COUNT 未満の
        署名は除外する(Phantom Guardrails 防止)。

        Args:
            feedback: run_suite の返り値。

        Returns:
            繰り返し失敗クラスタのリスト(count >= 2 のみ)。
        """
        signature_tasks: dict[str, list[str]] = {}
        signature_missing: dict[str, list[str]] = {}

        for result in feedback.per_task:
            if result.attribution != "harness" or not result.missing_points:
                continue
            signature = "|".join(sorted(result.missing_points))
            signature_tasks.setdefault(signature, [])
            if result.task_id not in signature_tasks[signature]:
                signature_tasks[signature].append(result.task_id)
            signature_missing[signature] = list(result.missing_points)

        clusters: list[FailureCluster] = []
        for signature, task_ids in signature_tasks.items():
            occurrences = sum(
                1
                for r in feedback.per_task
                if r.attribution == "harness" and r.missing_points
                and "|".join(sorted(r.missing_points)) == signature
            )
            if occurrences < _MIN_CLUSTER_COUNT:
                continue
            missing = signature_missing[signature]
            clusters.append(
                FailureCluster(
                    signature=signature,
                    task_ids=task_ids,
                    count=occurrences,
                    root_cause_hypothesis=self._hypothesize(missing),
                    attribution="harness",
                    sample_missing_points=missing,
                )
            )
        logger.info("failure clusters: %d (recurring only)", len(clusters))
        return clusters

    @staticmethod
    def _hypothesize(missing_points: list[str]) -> str:
        """不足点から根原因仮説ドラフトを作る。

        Args:
            missing_points: ジャッジが列挙した不足点。

        Returns:
            根原因仮説テキスト(既知テンプレートに一致しない場合は一般形)。
        """
        joined = " / ".join(missing_points)
        for keyword, hypothesis in _ROOT_CAUSE_TEMPLATES:
            if any(keyword in m for m in missing_points):
                return hypothesis
        return f"recurring output deficiencies: {joined[:120]}"
