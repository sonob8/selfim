"""Proposer の単体テスト(TP-5)。

検証観点(論文A §3.1/D.2 + 論文B Safe Guard 2):
- k件の提案を生成する
- 各提案は根原因引用 or 新規命名(排他)、target_module は1つ、predicted 非空
- プロンプトにSIM全文とD.2の8原則が含まれる
"""

from pathlib import Path

from selfim.evaluator import Evaluator
from selfim.models import build_model
from selfim.proposer import Proposer
from selfim.schemas import FailureCluster, SimRecord

import pytest


@pytest.fixture()
def sim_manager(tmp_path: Path):
    from selfim.sim_manager import SIMManager

    return SIMManager(path=tmp_path / "sim.json")


@pytest.fixture()
def proposer(sim_manager) -> Proposer:
    return Proposer(model=build_model("proposer", "mock"), sim_manager=sim_manager)


def _cluster() -> FailureCluster:
    """テスト用失敗クラスタを返す。"""
    return FailureCluster(
        signature="思考ステップ欠落: A|思考ステップ欠落: B",
        task_ids=["t1", "t2"],
        count=3,
        root_cause_hypothesis="required reasoning steps are omitted from the output",
        attribution="harness",
        sample_missing_points=["思考ステップ欠落: A", "思考ステップ欠落: B"],
    )


def _sources() -> dict[str, str]:
    """テスト用ハーネスソースを返す。"""
    return {"system_prompt": "ベース\n", "skill": "手順\n", "tool_code": "", "verify_code": ""}


class TestPropose:
    def test_generates_k_proposals_with_single_module(self, proposer: Proposer) -> None:
        proposals = proposer.propose(
            harness_sources=_sources(),
            clusters=[_cluster()],
            sim_record=SimRecord(),
            task_ids=["t1", "t2", "t3"],
            k=2,
        )
        assert len(proposals) == 2
        for p in proposals:
            # 根原因引用 or 新規命名の排他(schemasのバリデータで強制)
            assert (p.cites_problem is None) != (p.new_problem_name is None)
            assert p.target_module in ("system_prompt", "skill", "tool", "verify")
            assert len(p.predicted) >= 1

    def test_cites_existing_sim_problem_when_matched(self, proposer: Proposer, sim_manager) -> None:
        """SIMに同根原因のエントリがあれば既存IDを引用する(論文A D.2-3)。"""
        record = sim_manager.record_predicted(
            SimRecord(),
            __import__("selfim.schemas", fromlist=["Proposal"]).Proposal(
                new_problem_name="required reasoning steps are omitted from the output",
                target_module="system_prompt",
                what="過去の編集",
                new_content="x",
                predicted=["t1"],
            ),
            1, "s1",
        )
        proposals = proposer.propose(_sources(), [_cluster()], record, ["t1", "t2"], k=1)
        assert proposals[0].cites_problem == "P1"
        assert proposals[0].new_problem_name is None

    def test_new_problem_when_no_match(self, proposer: Proposer) -> None:
        proposals = proposer.propose(_sources(), [_cluster()], SimRecord(), ["t1"], k=1)
        assert proposals[0].new_problem_name == "required reasoning steps are omitted from the output"
        assert proposals[0].cites_problem is None

    def test_no_clusters_returns_empty(self, proposer: Proposer) -> None:
        assert proposer.propose(_sources(), [], SimRecord(), ["t1"], k=3) == []


class TestPromptContent:
    def test_prompt_contains_sim_and_d2_principles(self) -> None:
        """system prompt にSIM全文とD.2の8原則が含まれること(論文A §3.2, D.2)。"""
        from selfim.models import build_proposer_prompt

        sim_text = "[P1] required reasoning steps omitted (opened_round=1)"
        system, _user = build_proposer_prompt(
            harness_sources=_sources(),
            clusters=[_cluster()],
            sim_text=sim_text,
            task_ids=["t1"],
            k=2,
        )
        assert sim_text in system  # SIM全文(truncationなし)
        for principle in (
            "1. エントリは過去の経験として柔軟に扱う",
            "8. 似た設定ではタスク固有エビデンスを重視し、両方が変われば軽視する",
        ):
            assert principle in system
        assert "既存診断" in system  # D.2-3/-7 の中核語
