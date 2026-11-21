"""SIMManager の単体テスト(TP-1)。

検証観点:
- 新規根原因での record_predicted が新エントリを作る
- 既存ID引用で既存エントリに追記される(履歴保持)
- record_outcome が pending attempt を埋める
- 同名根原因の決定的マージ + aliases(3件以上の重複でも履歴を失わない)
- render_for_prompt が全文を含む(truncationなし)
"""

from pathlib import Path

from selfim.schemas import Proposal, SimProblem, SimRecord
from selfim.sim_manager import SIMManager

import pytest


@pytest.fixture()
def manager(tmp_path: Path) -> SIMManager:
    """空のSIMを束縛したSIMManagerを返す。"""
    return SIMManager(path=tmp_path / "sim.json")


def _proposal(cites: str | None = None, new_name: str | None = None,
              module: str = "system_prompt", what: str = "指示を追加") -> Proposal:
    """テスト用Proposalを生成するヘルパー。"""
    return Proposal(
        cites_problem=cites,
        new_problem_name=new_name,
        target_module=module,  # type: ignore[arg-type]
        what=what,
        new_content="new content",
        predicted=["legal-001", "legal-003"],
        rationale="過去試行と突き合わせた判断",
    )


class TestRecordPredicted:
    def test_new_problem_created(self, manager: SIMManager) -> None:
        record = SimRecord()
        updated = manager.record_predicted(record, _proposal(new_name="ステップ欠落"), round_no=1, sample_id="s1")

        assert len(updated.problems) == 1
        p = updated.problems[0]
        assert p.id == "P1"
        assert p.name == "ステップ欠落"
        assert p.opened_round == 1
        assert p.attempts[0].predicted == ["legal-001", "legal-003"]
        assert p.attempts[0].accepted is True
        assert updated.next_id == 2

    def test_existing_problem_appended(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="ステップ欠落"), 1, "s1")
        updated = manager.record_predicted(record, _proposal(cites="P1", what="指示を強化"), 2, "s2")

        p = updated.find_problem("P1")
        assert p is not None
        assert len(p.attempts) == 2
        # ラウンド1の試行は失われない(論文A: preserving earlier attempts)
        assert p.attempts[0].round == 1
        assert p.attempts[1].round == 2

    def test_attempt_is_pending_before_outcome(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="X"), 1, "s1")
        assert record.problems[0].attempts[0].backfilled is False
        assert record.problems[0].attempts[0].fixed == []
        assert manager.has_pending_outcome


class TestRecordOutcome:
    def test_outcome_fills_pending_attempt(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="X"), 1, "s1")
        updated = manager.record_outcome(record, fixed=["legal-001"], missed=["legal-003"], passed=7, total=10)

        a = updated.problems[0].attempts[0]
        assert a.fixed == ["legal-001"]
        assert a.missed == ["legal-003"]
        assert a.passed == 7
        assert a.total == 10
        assert a.backfilled is True
        assert not manager.has_pending_outcome

    def test_outcome_without_prediction_raises(self, manager: SIMManager) -> None:
        with pytest.raises(RuntimeError):
            manager.record_outcome(SimRecord(), fixed=[], missed=[], passed=0, total=0)


class TestMerge:
    def test_exact_name_merge_records_alias(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="ステップ欠落"), 1, "s1")
        # 同名の根原因を別IDで作る(Proposerが同名の新規命名をしたケース)
        duplicate = SimProblem(id="P2", name="ステップ欠落", opened_round=3, attempts=[])
        record = record.model_copy(update={
            "problems": [*record.problems, duplicate],
            "next_id": 3,
        })
        merged = manager.merge_by_exact_name(record)

        # P2 は P1 に吸収され、aliases に記録される
        assert merged.aliases == {"P2": "P1"}
        assert [p.id for p in merged.problems] == ["P1"]
        # 吸収先に試行が統合される(履歴は失われない)
        assert len(merged.find_problem("P1").attempts) == 1

    def test_three_way_merge_preserves_all_attempts(self, manager: SIMManager) -> None:
        """[FIX 2026-11-21] 同名3件のマージで中間の試行が喪失しないこと。

        過去バグ: 累積元を元のrecord側にしていたため、同名3件のマージで
        2件目の試行が上書き消滅した(diff-review 2026-11-21 で検出)。
        """
        base = manager.record_predicted(SimRecord(), _proposal(new_name="A", what="round1"), 1, "s1")
        # P2/P3 を同名"A"で作り、それぞれ別ラウンドの試行を1件ずつ持たせる
        from selfim.schemas import SimAttempt

        def with_attempt(problem_id: str, opened_round: int, rnd: int) -> SimProblem:
            a = SimAttempt(round=rnd, what=f"round{rnd}", files="x", predicted=["t"])
            return SimProblem(id=problem_id, name="A", opened_round=opened_round, attempts=[a])

        record = base.model_copy(update={
            "problems": [base.problems[0], with_attempt("P2", 2, 2), with_attempt("P3", 3, 3)],
            "next_id": 4,
        })

        merged = manager.merge_by_exact_name(record)

        assert merged.aliases == {"P2": "P1", "P3": "P1"}
        attempts = merged.find_problem("P1").attempts
        assert sorted(a.round for a in attempts) == [1, 2, 3]  # 全試行が保持される

    def test_find_problem_resolves_alias(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="X"), 1, "s1")
        record.aliases["P9"] = "P1"
        assert manager.find_problem_by_key(record, "P9") is record.problems[0]


class TestRender:
    def test_render_contains_all_entries_verbatim(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="根原因アルファ"), 1, "s1")
        record = manager.record_predicted(record, _proposal(new_name="根原因ベータ"), 2, "s1")
        text = manager.render_for_prompt(record)

        # 検索・truncationなしの全エントリ参照(論文A §3.2)
        assert "根原因アルファ" in text
        assert "根原因ベータ" in text
        assert "指示を追加" in text  # what の内容も含む
        assert "P1" in text and "P2" in text

    def test_save_and_load_roundtrip(self, manager: SIMManager) -> None:
        record = manager.record_predicted(SimRecord(), _proposal(new_name="X"), 1, "s1")
        manager.save(record)
        loaded = manager.load()
        assert loaded.model_dump() == record.model_dump()
