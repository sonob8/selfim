"""Proposer — 改善候補の生成(論文A §3.1/D.2 + 論文B Stage 2)。

責務:
- 現行ハーネス・失敗クラスタ・SIM全文を入力に k 件の候補を生成する
- Proposer(LLM)自身が「既存根原因の引用 or 新規根原因の命名」を判断する
  (論文A D.1: マッチング判断はProposer自身。類似度モデルは使わない)
- 1候補につき1モジュールのみ編集(論文B Safe Guard 1: Proposalスキーマで強制)

論文A D.2 の8原則はモデル側(models.build_proposer_prompt)のプロンプトに
埋め込まれる。
"""

from __future__ import annotations

import logging
from typing import Any

from selfim.models import ProposerModel
from selfim.schemas import FailureCluster, Proposal, SimRecord
from selfim.sim_manager import SIMManager

logger = logging.getLogger(__name__)


class Proposer:
    """提案モデルとSIMを束縛する。

    Attributes:
        model: ProposerModel 実装(mock または bedrock)。
        sim_manager: SIMのレンダリングに使うマネージャ。
    """

    def __init__(self, model: ProposerModel, sim_manager: SIMManager) -> None:
        """モデルとSIMマネージャを束縛する。

        Args:
            model: build_model("proposer", ...) の返り値。
            sim_manager: 現行runのSIMManager(render_for_prompt 用)。
        """
        self.model = model
        self.sim_manager = sim_manager

    def propose(
        self,
        harness_sources: dict[str, str],
        clusters: list[FailureCluster],
        sim_record: SimRecord,
        task_ids: list[str],
        k: int,
    ) -> list[Proposal]:
        """失敗クラスタとSIM全文から k 件の候補を生成する。

        Args:
            harness_sources: 現行ハーネスのモジュール名 → 内容。
            clusters: 繰り返し失敗クラスタ(count==1 は含まれない)。
            sim_record: 現在のSIM。
            task_ids: evolveセットの全タスクID(予測対象)。
            k: 生成する候補数。

        Returns:
            Proposal のリスト。クラスタがなければ空リスト(編集対象がない = 不採択)。
        """
        if not clusters:
            logger.info("no failure clusters -> no proposals")
            return []
        sim_text = self.sim_manager.render_for_prompt(sim_record)
        proposals = self.model.propose(
            harness_sources=harness_sources,
            clusters=clusters,
            sim_text=sim_text,
            task_ids=task_ids,
            k=k,
        )
        for i, p in enumerate(proposals):
            key = p.problem_key
            logger.info("proposal %d: module=%s cause=%s", i + 1, p.target_module, key)
        return proposals
