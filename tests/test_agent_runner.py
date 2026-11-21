"""config_loader / models / agent_runner の単体テスト(TP-3)。

検証観点:
- settings.yaml と .env の読込と既定値
- build_model("mock") が役割別クライアントを返す
- AgentRunner がモジュールからタスク出力を生成し、CallLogにtool呼出を記録する
- system prompt の指示(思考ステップ列挙・事実引用等)が出力に反映される
"""

from pathlib import Path

from selfim.agent_runner import AgentRunner, CallLog
from selfim.config_loader import load_settings
from selfim.models import build_model
from selfim.schemas import TaskSpec

import pytest


@pytest.fixture()
def seed_dir(tmp_path: Path) -> Path:
    """テスト用シードハーネスを作る。"""
    modules = tmp_path / "seed" / "modules"
    modules.mkdir(parents=True)
    (modules / "system_prompt.txt").write_text(
        "あなたは推論タスクを実行するエージェントです。\n", encoding="utf-8"
    )
    (modules / "skill.md").write_text("手順: 結論を最初に述べる。\n", encoding="utf-8")
    (modules / "tools.py").write_text(
        '"""シードツール。"""\n'
        "def outline(steps: list[str]) -> list[str]:\n"
        "    return steps\n",
        encoding="utf-8",
    )
    (modules / "verify.py").write_text(
        '"""完了前チェックリスト。"""\n'
        "def build_checklist(task_id: str) -> list[str]:\n"
        "    return [\"結論の再確認\"]\n",
        encoding="utf-8",
    )
    return tmp_path / "seed"


class TestConfigLoader:
    def test_load_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / "settings.yaml"
        cfg.write_text("n_rounds: 2\nk_candidates: 3\n", encoding="utf-8")
        s = load_settings(cfg)
        assert s.n_rounds == 2
        assert s.k_candidates == 3
        assert s.rollouts_per_task == 3  # 未指定キーは既定値
        assert s.diff_line_budget == 60

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_settings(tmp_path / "absent.yaml")


class TestBuildModel:
    def test_mock_client_by_role(self) -> None:
        for role in ("task", "judge", "proposer", "selector"):
            client = build_model(role=role, provider="mock")
            assert client.provider == "mock"
            assert client.role == role

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError):
            build_model(role="judge", provider="unknown-xyz")


class TestCallLog:
    def test_records_and_counts(self) -> None:
        log = CallLog()
        log.record("tools.py", "outline")
        log.record("tools.py", "outline")
        log.record("verify.py", "build_checklist")
        assert log.count_for("tools.py") == 2
        assert log.count_for("verify.py") == 1
        assert log.count_for("absent.py") == 0


class TestAgentRunnerMock:
    def test_run_task_output_contains_conclusion_and_checklist(self, seed_dir: Path) -> None:
        runner = AgentRunner(harness_dir=seed_dir, model=build_model("task", "mock"))
        task = TaskSpec(
            task_id="t1",
            prompt="判断せよ",
            context="事実: X社の契約",
            reasoning_steps_required=["ステップA", "ステップB"],
        )
        out = runner.run_task(task)
        assert out.call_log is not None
        assert "結論" in out.text  # skill.md の指示(結論を最初に)が反映される

    def test_prompt_instruction_enables_all_steps(self, seed_dir: Path) -> None:
        """system prompt に思考ステップ列挙の指示があれば全ステップが出る(mockの改善感受性)。"""
        prompt_file = seed_dir / "modules" / "system_prompt.txt"
        prompt_file.write_text(
            "あなたは推論タスクを実行するエージェントです。\n"
            "必ず各思考ステップを「ステップn:」として明示してください。\n",
            encoding="utf-8",
        )
        runner = AgentRunner(harness_dir=seed_dir, model=build_model("task", "mock"))
        task = TaskSpec(
            task_id="t1",
            prompt="判断せよ",
            context="",
            reasoning_steps_required=["要件確認", "該当性検討", "反対解釈", "整合チェック"],
        )
        out = runner.run_task(task)
        for step in task.reasoning_steps_required:
            assert step in out.text

    def test_base_prompt_omits_later_steps(self, seed_dir: Path) -> None:
        """シードのプロンプト(列挙指示なし)では後半のステップが欠落する。"""
        runner = AgentRunner(harness_dir=seed_dir, model=build_model("task", "mock"))
        task = TaskSpec(
            task_id="t1",
            prompt="判断せよ",
            context="",
            reasoning_steps_required=["要件確認", "該当性検討", "反対解釈", "整合チェック"],
        )
        out = runner.run_task(task)
        assert task.reasoning_steps_required[0] in out.text
        assert task.reasoning_steps_required[3] not in out.text  # 4番目のステップは出ない

    def test_tool_calls_logged_from_tools_module(self, seed_dir: Path) -> None:
        runner = AgentRunner(harness_dir=seed_dir, model=build_model("task", "mock"))
        task = TaskSpec(task_id="t1", prompt="判断せよ", context="", reasoning_steps_required=["A"])
        out = runner.run_task(task)
        assert out.call_log.count_for("tools.py") >= 1  # tools モジュールの実行が記録される
