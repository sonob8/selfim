"""schemas.py の単体テスト(TP-1 のスキーマ部分)。

検証観点:
- SIMスキーマが論文A D.3 の全フィールドをJSON roundtripで保持する
- Proposal の引用/新規命名の排他制約と predicted 非空制約
"""

from selfim.schemas import Proposal, SimAttempt, SimProblem, SimRecord

import pytest


def _sample_attempt() -> SimAttempt:
    """論文A Figure 9 のP5試行を模したサンプルを返す。"""
    return SimAttempt(
        round=6,
        sample="s2",
        accepted=True,
        backfilled=False,
        what="思考ステップ列挙の指示を system prompt に追加",
        files="system_prompt.txt",
        predicted=["legal-001", "legal-003"],
        fixed=["legal-001"],
        missed=["legal-003"],
        passed=7,
        total=10,
        domain="legal_judgment",
    )


class TestSimSchemaRoundtrip:
    """SIMのJSON roundtrip が D.3 の全フィールドを保持すること。"""

    def test_all_d3_fields_survive_roundtrip(self) -> None:
        record = SimRecord(
            next_id=2,
            carried_from={"arm": "run_a", "domain": "legal_judgment", "into": "run_b"},
            aliases={"P6": "P5"},
            problems=[
                SimProblem(
                    id="P5",
                    name="reasoning steps are omitted before the final verdict",
                    opened_round=6,
                    attempts=[_sample_attempt()],
                )
            ],
        )
        data = record.model_dump_json()
        restored = SimRecord.model_validate_json(data)

        assert restored.next_id == 2
        assert restored.carried_from == {
            "arm": "run_a",
            "domain": "legal_judgment",
            "into": "run_b",
        }
        assert restored.aliases == {"P6": "P5"}
        attempt = restored.problems[0].attempts[0]
        # D.3 の全 attempt フィールドを個別に検証する
        assert attempt.round == 6
        assert attempt.sample == "s2"
        assert attempt.accepted is True
        assert attempt.backfilled is False
        assert "system prompt" in attempt.what
        assert attempt.files == "system_prompt.txt"
        assert attempt.predicted == ["legal-001", "legal-003"]
        assert attempt.fixed == ["legal-001"]
        assert attempt.missed == ["legal-003"]
        assert attempt.passed == 7
        assert attempt.total == 10
        assert attempt.domain == "legal_judgment"

    def test_json_keys_match_d3_names(self) -> None:
        """JSONのキー名が D.3 の命名と完全一致すること(next_id/carried_from/aliases/problems)。"""
        data = SimRecord().model_dump()
        assert set(data.keys()) == {"next_id", "carried_from", "aliases", "problems"}


class TestProposalConstraints:
    """Proposal の排他制約と predicted 制約。"""

    def test_cites_and_new_name_are_exclusive(self) -> None:
        with pytest.raises(ValueError):
            Proposal(
                cites_problem="P1",
                new_problem_name="new cause",
                target_module="system_prompt",
                what="x",
                new_content="y",
                predicted=["t1"],
            )

    def test_neither_citation_nor_new_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            Proposal(
                target_module="system_prompt",
                what="x",
                new_content="y",
                predicted=["t1"],
            )

    def test_empty_predicted_rejected(self) -> None:
        with pytest.raises(ValueError):
            Proposal(
                new_problem_name="new cause",
                target_module="system_prompt",
                what="x",
                new_content="y",
                predicted=[],
            )

    def test_problem_key_prefers_citation(self) -> None:
        p = Proposal(
            cites_problem="P3",
            target_module="verify",
            what="x",
            new_content="y",
            predicted=["t1"],
        )
        assert p.problem_key == "P3"
