"""MainLoop の E2E テスト(TP-6、mockプロバイダでキーなし実行)。

検証観点:
- 2ラウンド k=2 でスコアが向上し、output/runs/<id>/ に sim.json(D.3形式)・
  評価ログ・採択済みハーネスが生成される
- 不採択ラウンドで sim.json がバイト等価に不変
- held-out before/after がレポートに記録される
"""

import json
from pathlib import Path

from selfim.config_loader import Settings, ModelRoleConfig
from selfim.evaluator import Evaluator
from selfim.main_loop import MainLoop
from selfim.models import build_model
from selfim.module_registry import ModuleRegistry
from selfim.proposer import Proposer
from selfim.selector import Selector
from selfim.schemas import SimRecord, TaskSpec
from selfim.sim_manager import SIMManager
from selfim.task_loader import load_suite
from selfim.validation_gate import ValidationGate

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_HARNESS = REPO_ROOT / "src" / "selfim" / "harness"


@pytest.fixture()
def suite(tmp_path: Path) -> tuple[list[TaskSpec], object]:
    """テスト用スイート(データは実ファイルだが output は tmp_path)を作る。"""
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "tasks.yaml").write_text(
        '- task_id: legal-001\n  prompt: 判断せよ\n  context: "事実: 契約の事案"\n'
        "  reasoning_steps_required: [要件確認, 催告検討, 反対解釈, 整合チェック]\n"
        '- task_id: legal-002\n  prompt: 判断せよ\n  context: "事実: 不法行為の事案"\n'
        "  reasoning_steps_required: [要件確認, 構成要件, 反対解釈, 整合チェック]\n"
        '- task_id: legal-003\n  prompt: 判断せよ\n  context: "事実: 相続の事案"\n'
        "  reasoning_steps_required: [相続人確定, 代襲検討, 反対解釈, 整合チェック]\n",
        encoding="utf-8",
    )
    (suite_dir / "rubric.yaml").write_text(
        "criteria:\n"
        "  - name: 思考ステップの完全性\n    weight: 0.4\n    description: x\n"
        "  - name: 結論の妥当性\n    weight: 0.3\n    description: x\n"
        "  - name: 根拠の具体性\n    weight: 0.2\n    description: x\n"
        "  - name: 反対解釈の検討\n    weight: 0.1\n    description: x\n",
        encoding="utf-8",
    )
    tasks, rubric = load_suite(suite_dir)
    return tasks, rubric


@pytest.fixture()
def loop_factory(tmp_path: Path, suite):
    """MainLoopを組み立てるファクトリ。"""
    tasks, rubric = suite

    def _factory() -> MainLoop:
        run_dir = tmp_path / "runs" / "run1"
        sim_manager = SIMManager(path=run_dir / "sim.json")
        return MainLoop(
            evaluator=Evaluator(model=build_model("judge", "mock")),
            proposer=Proposer(model=build_model("proposer", "mock"), sim_manager=sim_manager),
            selector=Selector(model=build_model("selector", "mock"), diff_line_budget=60),
            sim_manager=sim_manager,
            module_registry=ModuleRegistry(seed_dir=SEED_HARNESS, workspace_dir=run_dir / "harness"),
            gate=ValidationGate(diff_line_budget=60),
            task_model=build_model("task", "mock"),
            rubric=rubric,
            run_dir=run_dir,
            rollouts_per_task=2,
        )

    return _factory


class TestEndToEnd:
    def test_two_rounds_improve_and_write_outputs(self, loop_factory, suite, tmp_path: Path) -> None:
        """2ラウンドでスコアが向上し、成果物が揃うこと(成功基準2/3)。"""
        tasks, _rubric = suite
        loop = loop_factory()
        evolve = tasks[:2]
        held_out = tasks[2:]

        report = loop.run(tasks=tasks, splits=(evolve, held_out), n_rounds=2, k_candidates=2)

        # スコアが向上する(mockの改善感受性により round1 で採択があるはず)
        assert report.evolve_final > report.evolve_before
        assert report.held_out_before > 0 and report.held_out_after > 0

        run_dir = tmp_path / "runs" / "run1"
        # 成果物の存在確認
        sim_path = run_dir / "sim.json"
        assert sim_path.exists()
        assert (run_dir / "report.json").exists() is False  # report.json はCLIが書く(E2EではCLI経由)
        assert (run_dir / "evaluations").exists()
        assert (run_dir / "harness" / "modules" / "system_prompt.txt").exists()

        # sim.json が D.3 形式(next_id/carried_from/aliases/problems)であること
        sim_data = json.loads(sim_path.read_text(encoding="utf-8"))
        assert set(sim_data.keys()) == {"next_id", "carried_from", "aliases", "problems"}
        assert len(sim_data["problems"]) >= 1
        attempt = sim_data["problems"][0]["attempts"][0]
        assert set(attempt.keys()) == {
            "round", "sample", "accepted", "backfilled", "what", "files",
            "predicted", "fixed", "missed", "passed", "total", "domain",
        }
        # 予測→成果の2段階が記録されていること
        assert len(attempt["predicted"]) >= 1
        assert attempt["backfilled"] is True

        # 採択ラウンドの記録があること
        accepted_rounds = [r for r in report.rounds if r.accepted_candidate_id]
        assert len(accepted_rounds) >= 1

    def test_rejected_round_keeps_sim_byte_identical(self, loop_factory, suite, tmp_path: Path) -> None:
        """不採択ラウンドで sim.json がバイト等価に不変であること(成功基準3)。"""
        tasks, _rubric = suite
        loop = loop_factory()

        # 1ラウンド目で改善が起きる(採択)
        report1 = loop.run(tasks=tasks, splits=(tasks[:2], tasks[2:]), n_rounds=1, k_candidates=2)
        sim_path = tmp_path / "runs" / "run1" / "sim.json"
        content_after_round1 = sim_path.read_bytes()
        assert report1.rounds[0].accepted_candidate_id is not None

        # 改善後のハーネスは全ステップを出すため失敗クラスタが消え、
        # 2ラウンド目は提案なし = 不採択 = SIM不変
        report2 = loop.run(tasks=tasks, splits=(tasks[:2], tasks[2:]), n_rounds=1, k_candidates=2)
        assert report2.rounds[0].accepted_candidate_id is None
        assert sim_path.read_bytes() == content_after_round1

    def test_held_out_not_leaked_to_loop(self, loop_factory, suite, tmp_path: Path) -> None:
        """held-outタスクがループ内の予測・クラスタに現れないこと(成功基準8の下限確認)。"""
        tasks, _rubric = suite
        loop = loop_factory()
        report = loop.run(tasks=tasks, splits=(tasks[:2], tasks[2:]), n_rounds=2, k_candidates=2)

        manifest_dir = tmp_path / "runs" / "run1" / "evaluations"
        for manifest in manifest_dir.glob("manifest_round_*.json"):
            text = manifest.read_text(encoding="utf-8")
            assert "legal-003" not in text  # held-outタスクはmanifestに現れない
        assert report.held_out_before > 0
