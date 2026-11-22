"""src レイアウトのパッケージ解決と、docker 系テストの共通フィクスチャ。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """実Dockerデーモンが利用可能か(不可なら docker 系テストをスキップする)。

    docker CLI の有無と `docker info` の終了コードで判定する
    (CI 等デーモンなし環境では自動スキップ)。
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
