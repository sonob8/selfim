"""Evaluator の単体テスト(TP-4)。

検証観点:
- ルーブリック重み付き採点(0-100)
- 不足ステップが missing_points に列挙される
- attribution(harness/model)の導出
- cluster_failures: 同一署名の失敗を集約、count==1 は除外
"""

from pathlib import Path

from selfim.evaluator import Evaluator
from selfim.models import build_model
from selfim.schemas import Rubric, RubricCriterion, ScoringScale, TaskSpec

import pytest


@pytest.fixture()
def suite_dir(tmp_path: Path) -> Path:
    """最小のタスクスイートを作る。"""
    d = tmp_path / "suite"
    d.mkdir()
    (d / "tasks.yaml").write_text(
        "- task_id: t1\n  prompt: 判断せよ\n  reasoning_steps_required: [要件確認, 反対解釈]\n"
        "- task_id: t2\n  prompt: 判断せよ\n  reasoning_steps_required: [要件確認]\n",
        encoding="utf-8",
    )
    (d / "rubric.yaml").write_text(
        "scoring_scale: {min: 0, max: 100}\n"
        "criteria:\n"
        "  - name: 思考ステップの完全性\n    weight: 0.6\n    description: x\n"
        "  - name: 結論の妥当性\n    weight: 0.4\n    description: x\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture()
def evaluator() -> Evaluator:
    """mockジャッジを持つEvaluatorを返す。"""
    return Evaluator(model=build_model("judge", "mock"))


def _rubric() -> Rubric:
    """テスト用ルーブリックを返す。"""
    return Rubric(
        scoring_scale=ScoringScale(),
        criteria=[
            RubricCriterion(name="思考ステップの完全性", weight=0.5, description="x"),
            RubricCriterion(name="結論の妥当性", weight=0.5, description="x"),
        ],
    )


def _task(steps: list[str]) -> TaskSpec:
    """テスト用タスクを返す。"""
    return TaskSpec(task_id="t1", prompt="判断せよ", reasoning_steps_required=steps)


class TestEvaluate:
    def test_weighted_score_and_missing_points(self, evaluator: Evaluator) -> None:
        task = _task(["要件確認", "反対解釈", "整合チェック"])
        # 2/3ステップのみ出力された場合の決定的採点
        out = "結論: 判断します。\nステップ1: 要件確認 を確認した。\nステップ2: 反対解釈 を確認した。"
        result = evaluator.evaluate(out, task, _rubric())

        assert result.task_id == "t1"
        # 結論あり=100*0.5 + ステップ2/3=66.67*0.5
        assert result.score == pytest.approx(83.33, abs=0.01)
        assert result.missing_points == ["思考ステップ欠落: 整合チェック"]
        assert result.attribution == "harness"  # 欠落があるのでハーネス帰属

    def test_perfect_output_attributes_to_model(self, evaluator: Evaluator) -> None:
        task = _task(["要件確認"])
        out = "結論: 判断します。\nステップ1: 要件確認 を確認した。"
        result = evaluator.evaluate(out, task, _rubric())
        assert result.score == 100.0
        assert result.missing_points == []
        assert result.attribution == "model"  # 構造的欠落なし


class TestRunSuite:
    def test_run_suite_aggregates_scores(self, evaluator: Evaluator, suite_dir: Path) -> None:
        """run_suite がタスク×rolloutを評価し、集合スコアを返す。"""
        from selfim.agent_runner import AgentRunner
        from selfim.models import build_model as bm
        from selfim.task_loader import load_suite

        tasks, rubric_loaded = load_suite(suite_dir)
        # 最小ハーネス(mockタスクモデルが動くように必須モジュールを用意)
        harness = suite_dir / "harness" / "modules"
        harness.mkdir(parents=True)
        (harness / "system_prompt.txt").write_text("ベースプロンプト\n", encoding="utf-8")
        (harness / "skill.md").write_text("結論を最初に述べる。\n", encoding="utf-8")
        runner = AgentRunner(
            harness_dir=suite_dir / "harness", model=bm("task", "mock")
        )
        feedback = evaluator.run_suite(
            runner=runner, rubric=rubric_loaded, tasks=tasks, rollouts=2, round_no=0
        )
        assert feedback.label == "evolve"
        assert len(feedback.per_task) == len(tasks) * 2
        assert 0.0 <= feedback.score <= 100.0


class TestClusterFailures:
    def test_recurring_failures_clustered_singletons_excluded(self, evaluator: Evaluator) -> None:
        """同一署名の失敗を集約し、単発(count==1)はクラスタから除外する。"""
        from selfim.schemas import EvaluationResult, SuiteFeedback

        def res(task_id: str, missing: list[str]) -> EvaluationResult:
            return EvaluationResult(
                task_id=task_id, score=40.0, missing_points=missing, attribution="harness"
            )

        feedback = SuiteFeedback(
            label="evolve",
            per_task=[
                res("t1", ["思考ステップ欠落: A"]),
                res("t2", ["思考ステップ欠落: A"]),
                res("t3", ["思考ステップ欠落: A"]),
                res("t4", ["事実の引用による根拠付けが不足"]),  # 単発
            ],
        )
        clusters = evaluator.cluster_failures(feedback)

        assert len(clusters) == 1
        c = clusters[0]
        assert c.count == 3
        assert c.task_ids == ["t1", "t2", "t3"]
        assert "reasoning steps" in c.root_cause_hypothesis  # 根原因仮説は論文スタイルの英語
        assert all(t != "t4" for t in c.task_ids)  # 単発は除外

    def test_no_failures_returns_empty(self, evaluator: Evaluator) -> None:
        from selfim.schemas import EvaluationResult, SuiteFeedback

        feedback = SuiteFeedback(
            label="evolve",
            per_task=[EvaluationResult(task_id="t1", score=90.0, missing_points=[])],
        )
        assert evaluator.cluster_failures(feedback) == []
