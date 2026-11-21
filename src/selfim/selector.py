"""Selector — 実行前スクリーニングと評価後選択(論文A §3.1 + 論文B Stage 3)。

実行前スクリーニング(決定的規則。スクリーニング落ち候補は評価しない):
- タスク固有ヒント(new_content/what にタスクIDを直書き)の拒否(Safe Guard 2)
- 行数予算超過の拒否(編集は小さく)
- SIM上失敗済み方向の無根拠な再提案の拒否(論文A D.2-6)

評価後選択(モデル助言 + 決定的最終判定):
- ゲート通過候補のみ対象
- スコアがベースラインを超える候補があれば採択、なければ全不採択(Ht+1 = Ht)
"""

from __future__ import annotations

import logging

from selfim.models import SelectorModel
from selfim.schemas import (
    CandidateEvaluation,
    Proposal,
    ScreenResult,
    ScreeningDecision,
    SelectionResult,
    SimRecord,
)

logger = logging.getLogger(__name__)

# 再訪を許可する「新エビデンス」宣言のキーワード(論文A D.2-6)。
_NEW_EVIDENCE_KEYWORD = "新エビデンス"


class Selector:
    """選択モデルと予算を束縛する。

    Attributes:
        model: SelectorModel 実装(選択の助言部分)。
        diff_line_budget: スクリーニング段階の行数予算(Gateの最終判定は別)。
    """

    def __init__(self, model: SelectorModel, diff_line_budget: int) -> None:
        """モデルと予算を束縛する。

        Args:
            model: build_model("selector", ...) の返り値。
            diff_line_budget: 候補内容の行数上限(スクリーニング用の保守的下限)。
        """
        self.model = model
        self.diff_line_budget = diff_line_budget

    def screen(
        self,
        proposals: list[Proposal],
        sim_record: SimRecord,
        task_ids: list[str],
    ) -> ScreenResult:
        """実行前に候補をスクリーニングする(決定的規則)。

        Args:
            proposals: Proposer の候補リスト。
            sim_record: 現在のSIM(失敗済み方向の検出に使用)。
            task_ids: evolveセットのタスクID(タスク固有ヒント検出に使用)。

        Returns:
            ScreenResult(accepted と rejected)。
        """
        accepted: list[Proposal] = []
        rejected: list[ScreeningDecision] = []
        for p in proposals:
            reason = self._rejection_reason(p, sim_record, task_ids)
            if reason is None:
                accepted.append(p)
                logger.info("screening: accepted (%s)", p.problem_key)
            else:
                rejected.append(ScreeningDecision(proposal=p, accepted=False, reason=reason))
                logger.info("screening: rejected (%s): %s", p.problem_key, reason)
        return ScreenResult(accepted=accepted, rejected=rejected)

    def _rejection_reason(
        self, proposal: Proposal, sim_record: SimRecord, task_ids: list[str]
    ) -> str | None:
        """拒否すべき場合にその理由を返す。通過時は None を返す。

        Args:
            proposal: 判定対象候補。
            sim_record: 現在のSIM。
            task_ids: evolveセットのタスクID。

        Returns:
            拒否理由文字列、または None(通過)。
        """
        # 規則1: タスク固有ヒント(Safe Guard 2)。new_content/what にタスクID直書きは
        # 特定タスクのquirksへの過学習を意味する。
        for tid in task_ids:
            if tid in proposal.new_content or tid in proposal.what:
                return f"タスク固有ヒント: タスクID {tid} が編集内容に直書きされている"

        # 規則2: 行数予算(保守的な下限チェック。厳密なdiff行数はValidationGateが判定)
        content_lines = len(proposal.new_content.splitlines())
        if content_lines > self.diff_line_budget:
            return f"行数予算超過: {content_lines} 行 > 予算 {self.diff_line_budget} 行"

        # 規則3: 失敗済み方向の無根拠な再提案(論文A D.2-6)。
        # SIM内に「成果未確定(fixed==[])の同一 what の試行」があり、
        # 提案が新エビデンスを宣言していない場合は却下する。
        for problem in sim_record.problems:
            for attempt in problem.attempts:
                if not attempt.backfilled or attempt.fixed:
                    continue
                if attempt.what == proposal.what and _NEW_EVIDENCE_KEYWORD not in proposal.rationale:
                    return (
                        f"失敗済み方向の無根拠な再訪: {problem.id} round {attempt.round} は "
                        f"fixed=[] で未解決(新エビデンスの宣言が必要)"
                    )
        return None

    def select(
        self, base_score: float, evaluated: list[CandidateEvaluation]
    ) -> SelectionResult:
        """評価済み候補から採択を判断する。

        決定的最終判定: ゲート通過候補のみ対象とし、採択には
        スコア > ベースライン を要求する。モデルの助言がこの条件を
        満たさない候補を挙げた場合は無視する。

        Args:
            base_score: 現行ハーネスのevolveスコア。
            evaluated: 検証ゲートと再評価を経た候補。

        Returns:
            SelectionResult(採択IDまたは None = 全不採択)。
        """
        passed = [c for c in evaluated if c.gate.passed]
        if not passed:
            return SelectionResult(accepted_candidate_id=None, reason="ゲート通過候補なし")

        advisory = self.model.advisory_select(
            base_score=base_score,
            candidates=[
                {
                    "candidate_id": c.candidate_id,
                    "score": c.score,
                    "diff_lines": c.diff_lines,
                    "what": c.proposal.what,
                    "gate_passed": c.gate.passed,
                }
                for c in passed
            ],
            sim_text="",
        )
        valid_ids = {c.candidate_id for c in passed}
        if advisory.accepted_candidate_id not in valid_ids:
            return SelectionResult(
                accepted_candidate_id=None,
                reason=f"不採択: {advisory.reason}",
            )
        chosen = next(c for c in passed if c.candidate_id == advisory.accepted_candidate_id)
        if chosen.score <= base_score:
            return SelectionResult(
                accepted_candidate_id=None,
                reason=f"不採択: 最良候補 {chosen.score:.2f} がベースライン {base_score:.2f} 以下",
            )
        logger.info("selection: accepted %s (%.2f -> %.2f)", chosen.candidate_id, base_score, chosen.score)
        return SelectionResult(
            accepted_candidate_id=chosen.candidate_id,
            reason=f"採択: {base_score:.2f} -> {chosen.score:.2f} / {advisory.reason}",
        )
