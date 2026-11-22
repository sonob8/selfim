"""Bedrock 実経路の統合テスト(moto + converseエミュレータ使用)。

検証対象は「実strands Agent → 実BedrockModel → 実boto3シリアライズ → converse」の
経路。moto が AWS API を再現し、本テストのエミュレータ拡張(tests/bedrock_emulator.py)
が converse のみを補完する。APIキー・AWS認証情報は不要。

カバレッジ:
- StrandsJudgeModel.judge(構造化出力の toolUse パースを含む実経路)
- StrandsProposerModel.propose(SIM全文+D.2原則を含むプロンプト→構造化出力)
- StrandsTaskModel.run_task(プレーンテキスト応答 + verify/tool の CallLog 記録)
"""

from pathlib import Path
from typing import Any

import pytest

from bedrock_emulator import TEST_MODEL_ID, install_bedrock_converse_emulator

from selfim.agent_runner import AgentRunner, CallLog
from selfim.models import build_model
from selfim.schemas import (
    Rubric,
    RubricCriterion,
    TaskSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_HARNESS = REPO_ROOT / "src" / "selfim" / "harness"

# モンキーパッチはテストプロセス内で1回だけ行う(冪等)
install_bedrock_converse_emulator()

moto_mock = pytest.importorskip("moto", reason="moto が未インストール")


@pytest.fixture()
def bedrock_env():
    """mock_aws コンテキストを提供するフィクスチャ。"""
    with moto_mock.mock_aws():
        yield


def _model(role: str) -> Any:
    """bedrock プロバイダのモデルを組み立てる。"""
    return build_model(role, "bedrock", model_id=TEST_MODEL_ID, region="us-west-2")


def _task(steps: list[str]) -> TaskSpec:
    """テスト用タスクを返す。"""
    return TaskSpec(
        task_id="legal-001",
        prompt="判断せよ",
        context="事実: 契約の事案",
        reasoning_steps_required=steps,
    )


def _rubric() -> Rubric:
    """テスト用ルーブリックを返す。"""
    return Rubric(
        criteria=[
            RubricCriterion(name="思考ステップの完全性", weight=0.5, description="x"),
            RubricCriterion(name="結論の妥当性", weight=0.5, description="x"),
        ]
    )


class TestJudgeModel:
    def test_judge_returns_structured_evaluation(self, bedrock_env) -> None:
        """実strands経路の構造化出力が EvaluationResult に変換されること。"""
        judge = _model("judge")
        # ステップが2つしかない出力 → 欠落あり・harness帰属の決定的応答
        output = "結論: 判断します。\nステップ1: 要件確認\nステップ2: 該当性検討"
        result = judge.judge(_task(["要件確認", "該当性検討", "反対解釈", "整合チェック"]), _rubric(), output)

        assert result.task_id == "legal-001"
        assert result.score == 60.0
        assert result.attribution == "harness"
        assert any("思考ステップ" in m for m in result.missing_points)

    def test_judge_complete_output_scores_high(self, bedrock_env) -> None:
        """全ステップ+事実引用がある出力は満点で model 帰属になること。"""
        judge = _model("judge")
        output = (
            "結論: 判断します。\n"
            "ステップ1: 要件確認\nステップ2: 該当性検討\n"
            "ステップ3: 反対解釈\nステップ4: 整合チェック\n"
            "根拠(事実の引用): 事実: 契約の事案"
        )
        result = judge.judge(_task(["a", "b", "c", "d"]), _rubric(), output)
        assert result.score == 100.0
        assert result.attribution == "model"
        assert result.missing_points == []


class TestProposerModel:
    def test_propose_returns_proposal_list(self, bedrock_env) -> None:
        """実strands経路の提案が Proposal に変換されること(SIM全文はsystem側へ)。"""
        proposer = _model("proposer")
        proposals = proposer.propose(
            harness_sources={
                "system_prompt": "あなたは推論タスクを実行するエージェントです。\n",
                "skill": "手順\n",
                "tool_code": "",
                "verify_code": "",
            },
            clusters=[],
            sim_text="[P1] required reasoning steps are omitted from the output (opened_round=1)",
            task_ids=["legal-001"],
            k=1,
        )
        assert len(proposals) == 1
        p = proposals[0]
        # SIMに同根原因のエントリがあるため P1 を引用する(エミュレータ応答)
        assert p.cites_problem == "P1"
        assert p.target_module == "system_prompt"
        assert p.predicted == ["legal-001"]


class TestTaskModel:
    def test_run_task_returns_text_and_records_verify(self, bedrock_env) -> None:
        """実strands経路のタスク実行: テキスト応答と verify の CallLog 記録。"""
        model = _model("task")
        sources = {
            "system_prompt": "あなたは推論タスクを実行するエージェントです。\n",
            "skill": "結論を最初に述べる。\n",
            "tool_code": "",
            "verify_code": (
                "def build_checklist(task_id):\n"
                "    return [\"結論の再確認\"]\n"
            ),
        }
        call_log = CallLog()
        text = model.run_task(sources, _task(["a"]), call_log)

        assert "結論" in text
        # [FIX 2026-11-22] の検証: bedrock 経路でも verify 実行が記録されること
        assert call_log.count_for("verify.py") == 1

    def test_run_task_reflects_prompt_instructions(self, bedrock_env) -> None:
        """system prompt の指示(ステップ列挙・事実引用)が出力に反映されること。"""
        model = _model("task")
        sources = {
            "system_prompt": (
                "あなたは推論タスクを実行するエージェントです。\n"
                "必ず各思考ステップを「ステップn:」として明示してください。\n"
                "根拠には事実を引用してください。\n"
            ),
            "skill": "",
            "tool_code": "",
            "verify_code": "",
        }
        text = model.run_task(sources, _task(["a"]), CallLog())
        # 指示があると全4ステップの行が出る(各行に「ステップ」が2回現れるため行数で数える)
        step_lines = [ln for ln in text.splitlines() if ln.startswith("ステップ")]
        assert len(step_lines) == 4
        assert "根拠(事実の引用):" in text


class TestAgentRunnerWithBedrock:
    def test_execution_evidence_via_real_runner(self, bedrock_env, tmp_path: Path) -> None:
        """AgentRunner を通じた実行でエビデンス(prompt組込/verify呼出)が取れること。"""
        harness = tmp_path / "harness" / "modules"
        harness.mkdir(parents=True)
        (harness / "system_prompt.txt").write_text(
            "あなたは推論タスクを実行するエージェントです。\n", encoding="utf-8"
        )
        (harness / "skill.md").write_text("結論を最初に述べる。\n", encoding="utf-8")
        (harness / "verify.py").write_text(
            "def build_checklist(task_id):\n    return [\"結論の再確認\"]\n", encoding="utf-8"
        )
        runner = AgentRunner(harness_dir=tmp_path / "harness", model=_model("task"))
        out = runner.run_task(_task(["a"]))

        ev_prompt = runner.execution_evidence(out, "system_prompt")
        assert ev_prompt.prompt_included is True  # モジュール内容がプロンプトに組込まれた
        ev_verify = runner.execution_evidence(out, "verify")
        assert ev_verify.module_imported is True
        assert ev_verify.tool_calls == 1  # verify の実行が CallLog から確認できる
