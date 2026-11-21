"""ValidationGate の単体テスト(TP-2)。

検証観点(論文B Safe Guard 3 / Eq.(1) の G_val):
- Pythonモジュールの構文エラー候補を拒否する
- 非退行(候補スコア < ベースライン)を拒否する
- diff行数予算超過を拒否する
- 実行エビデンス欠落(tool呼出0件)を拒否する
- 全条件充足で合格する
"""

from pathlib import Path

from selfim.schemas import ExecutionEvidence, GateResult, Proposal
from selfim.validation_gate import ValidationGate

import pytest


@pytest.fixture()
def gate() -> ValidationGate:
    """diff行数予算10のValidationGateを返す。"""
    return ValidationGate(diff_line_budget=10)


def _proposal(module: str, content: str) -> Proposal:
    return Proposal(
        new_problem_name="X",
        target_module=module,  # type: ignore[arg-type]
        what="edit",
        new_content=content,
        predicted=["t1"],
    )


class TestGate:
    def test_pass_on_all_conditions_met(self, gate: ValidationGate) -> None:
        result = gate.check(
            proposal=_proposal("system_prompt", "prompt v2"),
            diff_lines=5,
            evidence=ExecutionEvidence(
                prompt_included=True, module_imported=True, tool_calls=0
            ),
            candidate_score=75.0,
            base_score=60.0,
        )
        assert result.passed is True
        assert result.reasons == []

    def test_reject_regression(self, gate: ValidationGate) -> None:
        result = gate.check(
            proposal=_proposal("system_prompt", "p"),
            diff_lines=5,
            evidence=ExecutionEvidence(prompt_included=True, module_imported=True, tool_calls=0),
            candidate_score=50.0,
            base_score=60.0,
        )
        assert result.passed is False
        assert any("退行" in r for r in result.reasons)

    def test_reject_diff_budget_exceeded(self, gate: ValidationGate) -> None:
        result = gate.check(
            proposal=_proposal("system_prompt", "p"),
            diff_lines=11,
            evidence=ExecutionEvidence(prompt_included=True, module_imported=True, tool_calls=0),
            candidate_score=70.0,
            base_score=60.0,
        )
        assert result.passed is False
        assert any("予算" in r for r in result.reasons)

    def test_reject_tool_module_without_execution(self, gate: ValidationGate) -> None:
        """tool/verifyモジュール編集は import成功 + tool呼出1件以上が必要(設計判断 D-3)。"""
        result = gate.check(
            proposal=_proposal("tool", "def f(): ..."),
            diff_lines=5,
            evidence=ExecutionEvidence(prompt_included=False, module_imported=False, tool_calls=0),
            candidate_score=70.0,
            base_score=60.0,
        )
        assert result.passed is False
        assert len(result.reasons) >= 2  # import失敗 + 呼出0件

    def test_reject_prompt_module_not_included(self, gate: ValidationGate) -> None:
        result = gate.check(
            proposal=_proposal("system_prompt", "p"),
            diff_lines=5,
            evidence=ExecutionEvidence(prompt_included=False, module_imported=True, tool_calls=0),
            candidate_score=70.0,
            base_score=60.0,
        )
        assert result.passed is False
        assert any("プロンプト" in r for r in result.reasons)

    def test_multiple_reasons_accumulated(self, gate: ValidationGate) -> None:
        """複数条件違反時は全理由を列挙する(デバッグ性のため)。"""
        result = gate.check(
            proposal=_proposal("tool", "def broken(:"),
            diff_lines=50,
            evidence=ExecutionEvidence(prompt_included=False, module_imported=False, tool_calls=0),
            candidate_score=10.0,
            base_score=60.0,
        )
        assert result.passed is False
        assert len(result.reasons) >= 3
