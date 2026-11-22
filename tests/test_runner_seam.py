"""TaskRunner シームの単体テスト(local/docker 実行系の差し替え点)。

検証観点:
- MainLoop が runner_factory 経由で実行器を組み立て、使用後に close() すること
- AgentOutput が envelope/error フィールド(既定値で既存互換)を持つこと
- build_execution_evidence が7モジュールすべてに同一セマンティクスで証拠を返すこと
"""

import json
from pathlib import Path

from selfim.agent_runner import AgentOutput, AgentRunner, CallLog, build_execution_evidence
from selfim.config_loader import ModelRoleConfig, Settings
from selfim.evaluator import Evaluator
from selfim.main_loop import MainLoop
from selfim.models import build_model
from selfim.module_registry import ModuleRegistry
from selfim.proposer import Proposer
from selfim.selector import Selector
from selfim.sim_manager import SIMManager
from selfim.task_loader import load_suite
from selfim.validation_gate import ValidationGate

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_HARNESS = REPO_ROOT / "src" / "selfim" / "harness"


class RecordingRunner:
    """AgentRunner を包み、close 呼出を記録するスタブ(TaskRunner Protocol 準拠)。"""

    created: list["RecordingRunner"] = []

    def __init__(self, harness_dir: Path, model: object) -> None:
        self.harness_dir = harness_dir
        self.inner = AgentRunner(harness_dir=harness_dir, model=model)
        self.closed = False
        RecordingRunner.created.append(self)

    def run_task(self, task):  # noqa: ANN001 - TaskSpec(テストでは実型不要)
        return self.inner.run_task(task)

    def execution_evidence(self, output, module):  # noqa: ANN001
        return self.inner.execution_evidence(output, module)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def suite(tmp_path: Path):
    """test_main_loop.py と同一の最小スイート。"""
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


class TestRunnerFactorySeam:
    def test_main_loop_uses_factory_and_closes_runners(self, suite, tmp_path: Path) -> None:
        """runner_factory 経由で実行器が組まれ、全実行器が close されること。"""
        tasks, rubric = suite
        task_model = build_model("task", "mock")
        run_dir = tmp_path / "runs" / "run1"
        sim_manager = SIMManager(path=run_dir / "sim.json")

        def factory(harness_dir: Path) -> RecordingRunner:
            return RecordingRunner(harness_dir=harness_dir, model=task_model)

        loop = MainLoop(
            evaluator=Evaluator(model=build_model("judge", "mock")),
            proposer=Proposer(model=build_model("proposer", "mock"), sim_manager=sim_manager),
            selector=Selector(model=build_model("selector", "mock"), diff_line_budget=60),
            sim_manager=sim_manager,
            module_registry=ModuleRegistry(seed_dir=SEED_HARNESS, workspace_dir=run_dir / "harness"),
            gate=ValidationGate(diff_line_budget=60),
            task_model=task_model,
            rubric=rubric,
            run_dir=run_dir,
            rollouts_per_task=1,
            runner_factory=factory,
        )

        RecordingRunner.created = []
        loop.run(tasks=tasks, splits=(tasks[:2], tasks[2:]), n_rounds=1, k_candidates=2)

        # ベースライン・ラウンド内再評価・候補ごとに実行器が作られ、すべて close される
        assert len(RecordingRunner.created) >= 3
        assert all(r.closed for r in RecordingRunner.created)
        # 候補ディレクトリに対する実行器が少なくとも1つあること
        assert any("candidates" in str(r.harness_dir) for r in RecordingRunner.created)


class TestAgentOutputExtension:
    def test_envelope_and_error_defaults(self) -> None:
        # 既存コンストラクタ呼出(4引数)がそのまま成立すること=既存互換
        out = AgentOutput(text="x", call_log=CallLog(), prompt="p")
        assert out.envelope is None
        assert out.error == ""
        assert out.tokens == 0


class TestBuildExecutionEvidence:
    """モジュール種別ごとの証拠テーブル(local/docker 共通セマンティクス)。"""

    def _workspace(self, tmp_path: Path) -> Path:
        """7モジュールを備えた最小ハーネスを作る。"""
        ws = tmp_path / "harness"
        (ws / "modules").mkdir(parents=True)
        (ws / "modules" / "system_prompt.txt").write_text("SP\n", encoding="utf-8")
        (ws / "modules" / "skill.md").write_text("SK\n", encoding="utf-8")
        (ws / "modules" / "tools.py").write_text("def outline(x):\n    return x\n", encoding="utf-8")
        (ws / "modules" / "verify.py").write_text("def build_checklist(t):\n    return []\n", encoding="utf-8")
        (ws / "scaffold").mkdir()
        (ws / "scaffold" / "entrypoint.py").write_text("def run_task(t):\n    return {}\n", encoding="utf-8")
        (ws / "scaffold" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
        (ws / "scaffold" / "requirements.txt").write_text("strands-agents\n", encoding="utf-8")
        return ws

    def test_content_modules_use_prompt_containment(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        output = AgentOutput(text="x", call_log=CallLog(), prompt="SP\n\nSK")
        ev = build_execution_evidence(output, "system_prompt", ws)
        assert ev.prompt_included is True
        assert ev.tool_calls == 0

    def test_code_modules_use_call_log(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        log = CallLog()
        log.record("tools.py", "outline")
        log.record("tools.py", "outline")
        output = AgentOutput(text="x", call_log=log, prompt="SP\n\nSK")
        ev = build_execution_evidence(output, "tool", ws)
        assert ev.prompt_included is False
        assert ev.module_imported is True
        assert ev.tool_calls == 2

    def test_scaffold_modules_use_build_and_smoke(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        output = AgentOutput(text="x", call_log=CallLog(), prompt="")
        for module in ("entrypoint", "dockerfile", "requirements"):
            ev = build_execution_evidence(output, module, ws, build_ok=True, smoke_ok=True)
            assert ev.prompt_included is False
            assert ev.module_imported is True  # build_ok
            assert ev.tool_calls == 1  # smoke_ok
        # ビルド失敗・スモーク失敗なら証拠なし(ゲート拒否側)
        ev = build_execution_evidence(output, "dockerfile", ws, build_ok=False, smoke_ok=False)
        assert ev.module_imported is False
        assert ev.tool_calls == 0
