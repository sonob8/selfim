"""config_loader — config/settings.yaml と .env の読み込み。

settings.yaml の未指定キーは既定値で補完する。シークレットは .env のみに置き
(グローバル規約)、settings.yaml には非秘密設定のみを書く。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelRoleConfig:
    """役割ごとのモデル設定。

    Attributes:
        provider: プロバイダ(None は SELFIM_MODEL_PROVIDER 環境変数に従う)。
    """

    provider: str | None = None


@dataclass(frozen=True)
class DockerSettings:
    """docker実行系の設定([DECISION 2026-11-22] AgentCore互換コンテナ実行の追加)。

    Attributes:
        container_port: コンテナ内ポート(AgentCore Runtime 規定の 8080)。
        ping_timeout_sec: 起動待ち(/ping が 200 を返すまで)の上限秒。
        ping_interval_sec: /ping のポーリング間隔秒。
        invocation_timeout_sec: 1回の /invocations のタイムアウト秒。
        build_timeout_sec: docker build のタイムアウト秒。
        keep_containers: true はコンテナを削除せず残す(デバッグ用)。

    注意: --network none / internal ネットワークはポート公開と両立しないため
    (検証 2026-11-22: どちらもホストからの接続が不能)、コンテナは通常ブリッジで
    起動する。候補コードの実行時外向き通信は完全には遮断できない残存リスクであり、
    docs/docker.md に記載する(ビルド時の持ち出しコマンドはSelectorが拒否する)。
    """

    container_port: int = 8080
    ping_timeout_sec: float = 60.0
    ping_interval_sec: float = 0.5
    invocation_timeout_sec: float = 120.0
    build_timeout_sec: int = 300
    keep_containers: bool = False


@dataclass(frozen=True)
class ShipSettings:
    """出荷ステップの設定。

    Attributes:
        enabled: ループ完了後のイメージ出荷(docker save + ship.json)を行うか。
    """

    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    """ループ全体の設定(config/settings.yaml 相当)。

    Attributes:
        n_rounds: 改善ラウンド数(論文A §4.1 は N=10)。
        k_candidates: 1ラウンドの候補数(論文A §4.1 は k=5)。
        rollouts_per_task: 1タスクあたりのrollout数(論文A Appendix B は 5)。
        diff_line_budget: ValidationGate の diff 行数上限。
        executor: 実行系("local" = in-process高速 / "docker" = AgentCore互換コンテナ)。
        docker: docker実行系の設定。
        ship: 出荷ステップの設定。
        models: 役割ごとのモデル設定(task/judge/proposer/selector)。
    """

    n_rounds: int = 3
    k_candidates: int = 3
    rollouts_per_task: int = 3
    diff_line_budget: int = 60
    token_budget: int | None = None
    executor: str = "local"
    docker: DockerSettings = field(default_factory=DockerSettings)
    ship: ShipSettings = field(default_factory=ShipSettings)
    models: dict[str, ModelRoleConfig] = field(default_factory=dict)

    def provider_for_role(self, role: str, env_default: str) -> str:
        """役割のプロバイダを解決する(役割設定 > 環境変数既定)。"""
        cfg = self.models.get(role)
        if cfg is not None and cfg.provider:
            return cfg.provider
        return env_default


# settings.yaml の既定値(ファイルに無いキーの補完に使う)
_DEFAULTS: dict[str, Any] = {
    "n_rounds": 3,
    "k_candidates": 3,
    "rollouts_per_task": 3,
    "diff_line_budget": 60,
    "token_budget": None,
    "executor": "local",
    "docker": {},
    "ship": {"enabled": True},
    "models": {},
}


def load_settings(path: Path) -> Settings:
    """settings.yaml を読み込み Settings を返す。

    Args:
        path: settings.yaml のパス。

    Returns:
        既定値とマージした Settings。

    Raises:
        FileNotFoundError: ファイルが存在しない場合。
        ValueError: YAMLが辞書でない場合。
    """
    if not path.exists():
        raise FileNotFoundError(f"settings.yaml が見つかりません: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("settings.yaml は辞書形式である必要があります")

    # [FIX 2026-11-22] executor のtypo(doker等)が黙って通過し、意図せずlocal実行に
    # なるのを防ぐため値を検証する(レビュー2026-11-22指摘)。
    executor = str(raw.get("executor", _DEFAULTS["executor"]))
    if executor not in ("local", "docker"):
        raise ValueError(f"executor は local / docker のいずれかです: {executor!r}")

    models_raw = raw.get("models", _DEFAULTS["models"])
    models: dict[str, ModelRoleConfig] = {}
    for role, cfg in models_raw.items():
        models[role] = ModelRoleConfig(provider=(cfg or {}).get("provider"))

    docker_raw = raw.get("docker", _DEFAULTS["docker"])
    docker_settings = DockerSettings(
        container_port=int(docker_raw.get("container_port", 8080)),
        ping_timeout_sec=float(docker_raw.get("ping_timeout_sec", 60.0)),
        ping_interval_sec=float(docker_raw.get("ping_interval_sec", 0.5)),
        invocation_timeout_sec=float(docker_raw.get("invocation_timeout_sec", 120.0)),
        build_timeout_sec=int(docker_raw.get("build_timeout_sec", 300)),
        keep_containers=bool(docker_raw.get("keep_containers", False)),
    )
    ship_raw = raw.get("ship", _DEFAULTS["ship"])

    raw_token_budget = raw.get("token_budget", _DEFAULTS["token_budget"])
    return Settings(
        n_rounds=int(raw.get("n_rounds", _DEFAULTS["n_rounds"])),
        k_candidates=int(raw.get("k_candidates", _DEFAULTS["k_candidates"])),
        rollouts_per_task=int(raw.get("rollouts_per_task", _DEFAULTS["rollouts_per_task"])),
        diff_line_budget=int(raw.get("diff_line_budget", _DEFAULTS["diff_line_budget"])),
        token_budget=int(raw_token_budget) if raw_token_budget is not None else None,
        executor=executor,
        docker=docker_settings,
        ship=ShipSettings(enabled=bool(ship_raw.get("enabled", True))),
        models=models,
    )
