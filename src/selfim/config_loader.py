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
class Settings:
    """ループ全体の設定(config/settings.yaml 相当)。

    Attributes:
        n_rounds: 改善ラウンド数(論文A §4.1 は N=10)。
        k_candidates: 1ラウンドの候補数(論文A §4.1 は k=5)。
        rollouts_per_task: 1タスクあたりのrollout数(論文A Appendix B は 5)。
        diff_line_budget: ValidationGate の diff 行数上限。
        models: 役割ごとのモデル設定(task/judge/proposer/selector)。
    """

    n_rounds: int = 3
    k_candidates: int = 3
    rollouts_per_task: int = 3
    diff_line_budget: int = 60
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

    models_raw = raw.get("models", _DEFAULTS["models"])
    models: dict[str, ModelRoleConfig] = {}
    for role, cfg in models_raw.items():
        models[role] = ModelRoleConfig(provider=(cfg or {}).get("provider"))

    return Settings(
        n_rounds=int(raw.get("n_rounds", _DEFAULTS["n_rounds"])),
        k_candidates=int(raw.get("k_candidates", _DEFAULTS["k_candidates"])),
        rollouts_per_task=int(raw.get("rollouts_per_task", _DEFAULTS["rollouts_per_task"])),
        diff_line_budget=int(raw.get("diff_line_budget", _DEFAULTS["diff_line_budget"])),
        models=models,
    )
