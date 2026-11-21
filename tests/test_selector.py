"""Selector の単体テスト(TP-5)。

検証観点(実行前スクリーニング + 評価後選択):
- タスク固有ヒント(タスクID直書き)を拒否する(論文B Safe Guard 2)
- 行数予算超過を拒否する
- SIM上失敗済み方向の無根拠な再提案を拒否する(論文A D.2-6)
- 複数モジュール横断の提案は schemas レベルで生成不可(スキーマテスト)
- 選択: スコア向上候補を採択 / 全候補が退行なら不採択
"""

from pathlib import Path

from selfim.models import build_model
from selfim.selector import Selector
from selfim.schemas import Proposal, SimAttempt, SimProblem, SimRecord

import pytest


@pytest.fixture()
def selector() -> Selector:
    return Selector(model=build_model("selector", "mock"), diff_line_budget=10)


def _proposal(content: str, what: str = "edit", cites: str | None = None) -> Proposal:
    return Proposal(
        cites_problem=cites,
        new_problem_name=None if cites else "new cause",
        target_module="system_prompt",
        what=what,
        new_content=content,
        predicted=["t1"],
    )


class TestScreen:
    def test_reject_task_specific_hint(self, selector: Selector) -> None:
        """タスクID直書きのヒントは却下する(論文B Safe Guard 2)。"""
        p = _proposal("指示です\nこれはタスク t1 専用の指示です。")
        result = selector.screen([p], sim_record=SimRecord(), task_ids=["t1", "t2"])
        assert p not in result.accepted
        assert any("タスク固有" in r.reason for r in result.rejected)

    def test_reject_diff_budget_exceeded(self, selector: Selector) -> None:
        """行数予算超過は却下する。"""
        big = "\n".join(f"line{i}" for i in range(30))
        p = _proposal("base\n" + big + "\n")
        result = selector.screen([p], sim_record=SimRecord(), task_ids=["t1"])
        assert p not in result.accepted
        assert any("予算" in r.reason for r in result.rejected)

    def test_reject_failed_direction_without_new_evidence(self, selector: Selector) -> None:
        """SIMで失敗済み(fixed=[] の試行)と同一内容の再提案は却下(新エビデンスなし)。"""
        record = SimRecord(
            problems=[
                SimProblem(
                    id="P1",
                    name="some cause",
                    opened_round=1,
                    attempts=[
                        SimAttempt(
                            round=1, what="過去の編集", files="system_prompt",
                            predicted=["t1"], fixed=[], missed=["t1"],
                            backfilled=True, domain="s",
                        )
                    ],
                )
            ]
        )
        # 失敗済み試行と同一の what を無根拠に再提案
        p = _proposal("new content", what="過去の編集")
        result = selector.screen([p], sim_record=record, task_ids=["t1"])
        assert p not in result.accepted
        assert any("再訪" in r.reason or "失敗" in r.reason for r in result.rejected)

    def test_allow_failed_direction_with_new_evidence(self, selector: Selector) -> None:
        """新エビデンスを明示した再提案は通す(論文A D.2-6)。"""
        record = SimRecord(
            problems=[
                SimProblem(
                    id="P1",
                    name="some cause",
                    opened_round=1,
                    attempts=[
                        SimAttempt(
                            round=1, what="過去の編集", files="system_prompt",
                            predicted=["t1"], fixed=[], missed=["t1"],
                            backfilled=True, domain="s",
                        )
                    ],
                )
            ]
        )
        p = Proposal(
            cites_problem="P1",
            target_module="system_prompt",
            what="過去の編集",
            new_content="new content v2",
            predicted=["t1"],
            rationale="新エビデンス: 評価ログの署名が変化したため再訪する",
        )
        result = selector.screen([p], sim_record=record, task_ids=["t1"])
        assert p in result.accepted

    def test_clean_proposal_passes(self, selector: Selector) -> None:
        p = _proposal("指示を追加した内容\n")
        result = selector.screen([p], sim_record=SimRecord(), task_ids=["t1"])
        assert result.accepted == [p]
        assert result.rejected == []


class TestSelect:
    def test_accept_best_improvement(self, selector: Selector) -> None:
        from selfim.schemas import CandidateEvaluation, GateResult

        cands = [
            CandidateEvaluation(candidate_id="s1", proposal=_proposal("a"), gate=GateResult(passed=True), score=70.0, diff_lines=5),
            CandidateEvaluation(candidate_id="s2", proposal=_proposal("b"), gate=GateResult(passed=True), score=80.0, diff_lines=8),
        ]
        result = selector.select(base_score=60.0, evaluated=cands)
        assert result.accepted_candidate_id == "s2"

    def test_reject_when_no_improvement(self, selector: Selector) -> None:
        from selfim.schemas import CandidateEvaluation, GateResult

        cands = [
            CandidateEvaluation(candidate_id="s1", proposal=_proposal("a"), gate=GateResult(passed=True), score=55.0, diff_lines=5),
        ]
        result = selector.select(base_score=60.0, evaluated=cands)
        assert result.accepted_candidate_id is None

    def test_reject_gate_failed_candidates(self, selector: Selector) -> None:
        from selfim.schemas import CandidateEvaluation, GateResult

        cands = [
            CandidateEvaluation(
                candidate_id="s1", proposal=_proposal("a"),
                gate=GateResult(passed=False, reasons=["退行"]), score=90.0, diff_lines=5,
            ),
        ]
        result = selector.select(base_score=60.0, evaluated=cands)
        assert result.accepted_candidate_id is None


class TestSchemaMultiModule:
    def test_multi_module_proposal_cannot_exist(self) -> None:
        """複数モジュール横断編集は Proposal スキーマが生成を拒否する(SG-1)。"""
        with pytest.raises(ValueError):
            Proposal.model_validate({
                "new_problem_name": "x",
                "target_module": ["system_prompt", "tool"],  # リストは不可
                "what": "x",
                "new_content": "y",
                "predicted": ["t1"],
            })
