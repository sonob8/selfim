"""Selector — 実行前スクリーニングと評価後選択(論文A §3.1 + 論文B Stage 3)。

実行前スクリーニング(決定的規則。スクリーニング落ち候補は評価しない):
- タスク固有ヒント(new_content/what にタスクIDを直書き)の拒否(Safe Guard 2)
- 行数予算超過の拒否(編集は小さく)
- SIM上失敗済み方向の無根拠な再提案の拒否(論文A D.2-6)
- デプロイ境界の安全規則(論文B Table III): 評価データ・論文のイメージ持ち込み、
  ホストパスマウント、外部持ち出しの拒否
- local実行系では足場モジュール(entrypoint/dockerfile/requirements)編集の拒否
  (ビルド・スモーク検証が行えないため、未検証の実行証拠でゲートを通るのを防ぐ)

評価後選択(モデル助言 + 決定的最終判定):
- ゲート通過候補のみ対象
- スコアがベースラインを超える候補があれば採択、なければ全不採択(Ht+1 = Ht)
"""

from __future__ import annotations

import logging
import re

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

# 足場モジュール(検証にdocker実行系が必要なモジュール)。
_SCAFFOLD_MODULES = ("entrypoint", "dockerfile", "requirements")

# [DECISION 2026-11-22] Dockerfile 編集の禁止パターン(論文B Table III のコード強制)。
# 評価データ(data/)・論文(papers/)をイメージへ持ち込む COPY/ADD、親ディレクトリ
# 参照、ホストパスのバインド、外部へのデータ持ち出し疑いのある RUN を拒否する。
# 構造的な破壊(COPY runtime/ の削除等)は下流の docker build 失敗で検出される。
_DOCKERFILE_FORBIDDEN: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?im)^\s*(COPY|ADD)\s+\S*(data|papers)\b"),
        "評価データ・論文の COPY/ADD を含む(論文B Table III 違反)",
    ),
    (
        re.compile(r"(?im)^\s*(COPY|ADD)\s+\.\."),
        "親ディレクトリ参照を含む COPY/ADD(コンテキスト外の持ち込み)",
    ),
    (
        re.compile(r"--mount"),
        "ホストパスのバインドマウントを含む(論文B Table III 違反)",
    ),
    (
        re.compile(r"(?im)^\s*RUN\s+.*\b(aws\s+s3|curl|wget)\b"),
        "外部へのデータ持ち出し疑いのある RUN を含む",
    ),
]

# entrypoint 契約の存在確認用(固定ランタイムが呼び出す関数シグネチャ)。
_RUN_TASK_PATTERN = re.compile(r"def\s+run_task\s*\(")


class Selector:
    """選択モデルと予算を束縛する。

    Attributes:
        model: SelectorModel 実装(選択の助言部分)。
        diff_line_budget: スクリーニング段階の行数予算(Gateの最終判定は別)。
    """

    def __init__(self, model: SelectorModel, diff_line_budget: int, executor: str = "local") -> None:
        """モデルと予算を実行系名を束縛する。

        Args:
            model: build_model("selector", ...) の返り値。
            diff_line_budget: 候補内容の行数上限(スクリーニング用の保守的下限)。
            executor: 実行系名("local" / "docker")。docker のみ足場モジュール
                (entrypoint/dockerfile/requirements)の編集を検証できるため、
                local では足場編集候補をスクリーニング段階で拒否する
                ([DECISION 2026-11-22] 未検証の実行証拠でゲートを通るのを防ぐ)。
        """
        self.model = model
        self.diff_line_budget = diff_line_budget
        self.executor = executor

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

        # 規則4: デプロイ境界の安全規則(論文B Table III のコード強制)。
        if proposal.target_module == "dockerfile":
            for pattern, reason in _DOCKERFILE_FORBIDDEN:
                if pattern.search(proposal.new_content):
                    return f"Dockerfile 安全違反: {reason}"

        # 規則5: entrypoint 契約(固定ランタイムが呼ぶ run_task が存在しないと
        # 全タスクが失敗する)。深い破壊はスモークテストで検出する。
        if proposal.target_module == "entrypoint" and not _RUN_TASK_PATTERN.search(
            proposal.new_content
        ):
            return "entrypoint 契約違反: run_task(task: dict) -> dict 関数が存在しない"

        # 規則6: 足場モジュールはdocker実行系でのみ検証できる。
        # local実行系ではビルド・スモーク相当の証拠が取れないため拒否する
        # (未検証コードのゲート通過防止、論文B SG-3)。
        if proposal.target_module in _SCAFFOLD_MODULES and self.executor != "docker":
            return (
                f"足場モジュール({proposal.target_module})の編集はdocker実行系でのみ"
                "検証できる(local実行系ではビルド・スモーク証拠を取得できない)"
            )
        return None

    def select(
        self,
        base_score: float,
        evaluated: list[CandidateEvaluation],
        sim_text: str = "",
    ) -> SelectionResult:
        """評価済み候補から採択を判断する。

        決定的最終判定: ゲート通過候補のみ対象とし、採択には
        スコア > ベースライン を要求する。モデルの助言がこの条件を
        満たさない候補を挙げた場合は無視する。

        Args:
            base_score: 現行ハーネスのevolveスコア。
            evaluated: 検証ゲートと再評価を経た候補。
            sim_text: SIM全文(render_for_prompt の出力)。論文A §3.2 に従い
                セレクタもSIM全文を参照して採択判断する(D.2-8 の
                「似た設定ではタスク固有エビデンスを重視」判断に必要)。

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
            sim_text=sim_text,
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
