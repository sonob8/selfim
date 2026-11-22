"""Shipper — ループ完了後の最終イメージ出荷(ローカルイメージ + tar + メタデータ)。

責務:
- 昇格済みワークスペースから最終イメージ(selfim-<run_id>-final)をビルドする
- docker save の結果を <run_dir>/ship/agent-image.tar へ書き出す
- 出荷メタデータ(ship.json: タグ・digest・モジュールハッシュ・スコア・SIM概要)を書く

[DECISION 2026-11-22] 出荷成果物はローカルイメージ+tar に限定する(ユーザー決定)。
ECR 等への push は将来スクリプトとして追加する。ship ディレクトリは
harness_dir の親(run_dir)直下の ship/ とする。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from selfim.module_registry import MODULE_PATHS
from selfim.schemas import RunReport, ShipManifest, SimRecord

logger = logging.getLogger(__name__)

# tar・メタデータのファイル名(出荷成果物の固定名)。
_TAR_NAME = "agent-image.tar"
_MANIFEST_NAME = "ship.json"


def _module_hashes(harness_dir: Path) -> dict[str, str]:
    """出荷時点の全モジュールの sha256 を返す(ハーネス内容の指紋)。

    Args:
        harness_dir: 昇格済みワークスペース。

    Returns:
        モジュール名 → sha256 hex(16進64文字)。
    """
    hashes: dict[str, str] = {}
    for name, rel in MODULE_PATHS.items():
        p = harness_dir / rel
        if p.exists():
            hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


class Shipper:
    """最終イメージのビルドと出荷成果物の書き出しを行う。"""

    def __init__(self, client: Any = None) -> None:
        """docker クライアントを束縛する(None なら ImageBuilder 経由で遅延初期化)。

        Args:
            client: docker SDK クライアント(テストではfakeを注入)。
        """
        self._client = client

    def _ensure_client(self) -> Any:
        """クライアントを遅延初期化して返す。"""
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def ship(
        self,
        harness_dir: Path,
        report: RunReport,
        sim_record: SimRecord,
        run_id: str,
        executor: str,
        model_provider: str,
        args: dict[str, Any] | None = None,
    ) -> ShipManifest | None:
        """最終イメージをビルドし、出荷成果物を書き出す。

        Args:
            harness_dir: 昇格済みワークスペース(ビルドコンテキスト)。
            report: ループの実行レポート(スコア記録用)。
            sim_record: 最終SIM(問題数の記録用)。
            run_id: run識別子(イメージタグに使う)。
            executor: 実行系名(local / docker)。
            model_provider: タスクモデルプロバイダ。
            args: 実行時引数(スイート名・ラウンド数等、ship.json に記録する)。

        Returns:
            書き出した ShipManifest。ビルド失敗時は None(例外を投げない:
            出荷失敗でループ成果(report.json 等)を失わせないため)。
        """
        tag = f"selfim-{run_id}-final"
        client = self._ensure_client()
        try:
            # Dockerfile は scaffold/Dockerfile にあるため dockerfile 相対パスを明示する
            client.images.build(
                path=str(harness_dir), tag=tag, rm=True, dockerfile=MODULE_PATHS["dockerfile"]
            )
        except Exception as exc:  # noqa: BLE001 - 出荷失敗は警告に留める
            logger.error("最終イメージのビルドに失敗しました: %s: %s", tag, exc)
            return None

        image = client.images.get(tag)
        digest = str(getattr(image, "id", ""))[: len("sha256:") + 12]

        ship_dir = harness_dir.parent / "ship"
        ship_dir.mkdir(parents=True, exist_ok=True)
        tar_path = ship_dir / _TAR_NAME
        with tar_path.open("wb") as f:
            for chunk in image.save():
                f.write(chunk)

        manifest = ShipManifest(
            image_tag=tag,
            image_digest=digest,
            tar_path=str(tar_path),
            module_hashes=_module_hashes(harness_dir),
            evolve_before=report.evolve_before,
            evolve_final=report.evolve_final,
            held_out_before=report.held_out_before,
            held_out_after=report.held_out_after,
            sim_problem_count=len(sim_record.problems),
            executor=executor,
            model_provider=model_provider,
            args=dict(args or {}),
        )
        (ship_dir / _MANIFEST_NAME).write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("[ship] image=%s tar=%s", tag, tar_path)
        return manifest
