"""Shipper の単体テスト(出荷ステップ: 最終イメージのタグ付け・tar保存・メタデータ書き出し)。

検証観点(fake DockerClient 使用・実Docker不要):
- 昇格済みワークスペースから selfim-<run_id>-final イメージをビルドすること
- docker save の結果を ship/agent-image.tar へ書き出すこと
- ship.json(ShipManifest)にタグ・digest・モジュールハッシュ・スコア・SIM問題数が
  記録されること
- ビルド失敗時は例外を投げず None を返すこと(ループ全体を落とさない)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from selfim.schemas import RunReport, SimProblem, SimRecord

import pytest


class FakeShipImage:
    """docker SDK Image の最小fake(save 可能)。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.id = "sha256:" + "f" * 64

    def save(self) -> Any:
        return iter([b"FAKE-TAR-BYTES"])


class FakeShipImages:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.build_calls: list[dict[str, Any]] = []

    def build(self, **kwargs: Any) -> tuple[FakeShipImage, Any]:
        self.build_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("pip resolution failed")
        return FakeShipImage(kwargs.get("tag", "")), iter([])

    def get(self, tag: str) -> FakeShipImage:
        return FakeShipImage(tag)


class FakeShipClient:
    def __init__(self, fail: bool = False) -> None:
        self.images = FakeShipImages(fail=fail)


def _report() -> RunReport:
    return RunReport(
        run_id="run1",
        suite="legal_judgment",
        provider="mock",
        n_rounds=2,
        k_candidates=2,
        held_out_before=50.0,
        held_out_after=70.0,
        evolve_before=40.0,
        evolve_final=80.0,
    )


def _sim_record() -> SimRecord:
    return SimRecord(
        next_id=2,
        problems=[SimProblem(id="P1", name="cause", opened_round=1)],
    )


class TestShipper:
    def test_ship_builds_final_image_and_writes_artifacts(self, tmp_path: Path) -> None:
        from selfim.shipper import Shipper

        # 出荷対象ハーネス(昇格済みワークスペースの代わり)
        harness = tmp_path / "harness"
        (harness / "modules").mkdir(parents=True)
        (harness / "modules" / "system_prompt.txt").write_text("SP\n", encoding="utf-8")
        (harness / "scaffold").mkdir()
        (harness / "scaffold" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")

        client = FakeShipClient()
        shipper = Shipper(client=client)
        manifest = shipper.ship(
            harness_dir=harness,
            report=_report(),
            sim_record=_sim_record(),
            run_id="run1",
            executor="docker",
            model_provider="mock",
            args={"suite": "legal_judgment", "rounds": 2},
        )

        # 最終イメージのビルド(タグ selfim-<run_id>-final)
        assert client.images.build_calls[0]["tag"] == "selfim-run1-final"
        # tar と ship.json の書き出し
        ship_dir = manifest_tar_dir = tmp_path / "ship"
        tar_path = ship_dir / "agent-image.tar"
        assert tar_path.exists()
        assert tar_path.read_bytes() == b"FAKE-TAR-BYTES"
        ship_json = ship_dir / "ship.json"
        assert ship_json.exists()

        data = json.loads(ship_json.read_text(encoding="utf-8"))
        assert data["image_tag"] == "selfim-run1-final"
        assert data["executor"] == "docker"
        assert data["evolve_before"] == 40.0
        assert data["evolve_final"] == 80.0
        assert data["held_out_before"] == 50.0
        assert data["held_out_after"] == 70.0
        assert data["sim_problem_count"] == 1
        assert data["module_hashes"]["system_prompt"]
        # manifest の内容と ship.json が一致すること
        assert manifest.image_tag == data["image_tag"]
        assert manifest.tar_path.endswith("agent-image.tar")

    def test_ship_returns_none_on_build_failure(self, tmp_path: Path) -> None:
        """ビルド失敗で例外を投げず None(呼び出し側が警告ログを出す契約)。"""
        from selfim.shipper import Shipper

        harness = tmp_path / "harness"
        harness.mkdir()
        shipper = Shipper(client=FakeShipClient(fail=True))
        manifest = shipper.ship(
            harness_dir=harness,
            report=_report(),
            sim_record=SimRecord(),
            run_id="run1",
            executor="docker",
            model_provider="mock",
            args={},
        )
        assert manifest is None
