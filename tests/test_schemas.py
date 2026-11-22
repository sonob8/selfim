"""schemas.py の単体テスト(TP-1 のスキーマ部分)。

検証観点:
- SIMスキーマが論文A D.3 の全フィールドをJSON roundtripで保持する
- Proposal の引用/新規命名の排他制約と predicted 非空制約
- Docker実行系のInvocationEnvelope系スキーマ(JSON往復)とModuleNameの7値化
"""

from selfim.schemas import (
    InvocationEnvelope,
    ModuleName,
    Proposal,
    ShipManifest,
    SimAttempt,
    SimProblem,
    SimRecord,
    TokenUsage,
)

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


class TestModuleName:
    """ModuleName がDocker足場を含む7モジュールに拡張されていること。"""

    def test_all_seven_modules_are_valid_targets(self) -> None:
        # [DECISION 2026-11-22] 足場(entrypoint/dockerfile/requirements)も編集対象化。
        # typing.get_args で Literal の全許容値を検証する。
        from typing import get_args

        assert set(get_args(ModuleName)) == {
            "system_prompt",
            "skill",
            "tool",
            "verify",
            "entrypoint",
            "dockerfile",
            "requirements",
        }

    def test_proposal_accepts_scaffold_module(self) -> None:
        p = Proposal(
            new_problem_name="dependency missing in image",
            target_module="requirements",
            what="パッケージ追加",
            new_content="strands-agents>=1.65.0\n",
            predicted=["t1"],
        )
        assert p.target_module == "requirements"


class TestInvocationEnvelope:
    """コンテナ⇔ホスト間の往復ペイロード(InvocationEnvelope)のスキーマ。

    検証観点:
    - レスポンスJSON形式(ok/output_text/composed_prompt/call_log/token_usage/error)の解析
    - 呼び出し失敗(ok=False, error入り)でもパースできる
    - 既定値で空のエンベロープが作れる(失敗時のフォールバック用)
    """

    def test_parse_full_response_json(self) -> None:
        payload = {
            "ok": True,
            "output_text": "結論: ...",
            "composed_prompt": "あなたは裁判官...",
            "call_log": [
                {"module_file": "tools.py", "function_name": "outline"},
                {"module_file": "verify.py", "function_name": "build_checklist"},
            ],
            "token_usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            "error": "",
        }
        env = InvocationEnvelope.model_validate(payload)

        assert env.ok is True
        assert env.output_text == "結論: ..."
        assert env.composed_prompt == "あなたは裁判官..."
        assert [e.function_name for e in env.call_log] == ["outline", "build_checklist"]
        assert env.token_usage.total_tokens == 30

    def test_roundtrip_preserves_fields(self) -> None:
        env = InvocationEnvelope(
            ok=False,
            output_text="",
            error="container exited",
            token_usage=TokenUsage(input_tokens=1, output_tokens=2, total_tokens=3),
        )
        restored = InvocationEnvelope.model_validate_json(env.model_dump_json())

        assert restored.ok is False
        assert restored.error == "container exited"
        assert restored.token_usage.output_tokens == 2
        assert restored.call_log == []

    def test_defaults_are_empty(self) -> None:
        env = InvocationEnvelope()
        assert env.ok is True
        assert env.output_text == ""
        assert env.call_log == []
        assert env.token_usage.total_tokens == 0
        assert env.error == ""


class TestShipManifest:
    """出荷成果物のメタデータ(ship.json)のスキーマ。"""

    def test_manifest_serializes_all_fields(self) -> None:
        manifest = ShipManifest(
            image_tag="selfim-run1-final",
            image_digest="sha256:abc123",
            tar_path="output/runs/run1/ship/agent-image.tar",
            module_hashes={"system_prompt": "deadbeef", "dockerfile": "cafebabe"},
            evolve_before=40.0,
            evolve_final=80.0,
            held_out_before=45.0,
            held_out_after=75.0,
            sim_problem_count=3,
            executor="docker",
            model_provider="mock",
            args={"suite": "legal_judgment", "rounds": 2},
        )
        restored = ShipManifest.model_validate_json(manifest.model_dump_json())

        assert restored.image_tag == "selfim-run1-final"
        assert restored.module_hashes["dockerfile"] == "cafebabe"
        assert restored.evolve_final == 80.0
        assert restored.args["rounds"] == 2
