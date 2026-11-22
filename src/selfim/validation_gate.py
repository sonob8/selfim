"""ValidationGate — 採択前の受け入れチェック(論文B Safe Guard 3、Eq.(1) の G_val)。

全条件を満たさない候補は拒否する:
1. Pythonモジュールの構文確認(tool/verify/entrypoint。compile + import相当)
2. docker実行系のビルド成否(足場モジュールの構文/import相当、SG-3 の実行時版)
3. docker実行系のスモーク成否(新コードパス実行の実行時確認、SG-3)
4. 候補が実タスクで実行されたことのエビデンス(設計判断 D-3: 種別エビデンス)
5. 非退行(candidate_score >= base_score)
6. diff行数予算内(Safe Guard 2: 編集は小さく)
7. トークン予算内(Eq.(1) の B_tok。上限設定時のみ)

違反は1件で打ち切らず全理由を列挙する(デバッグ性のため)。
"""

from __future__ import annotations

import logging

from selfim.schemas import ExecutionEvidence, GateResult, Proposal

logger = logging.getLogger(__name__)

# tool/verify モジュール編集時に要求される最小tool呼出数。
# 0件なら新コードパスが一度も走っていないことを意味する(Dead code 防止)。
_MIN_TOOL_CALLS = 1


class ValidationGate:
    """diff行数予算とトークン予算を束縛し、候補の受け入れ判定を行う。

    Attributes:
        diff_line_budget: 1候補あたりのdiff追加/削除行数上限。
        token_budget: 1タスクあたりの実行トークン上限(None は上限なし)。
    """

    def __init__(self, diff_line_budget: int, token_budget: int | None = None) -> None:
        """予算を束縛する。

        Args:
            diff_line_budget: diff行数上限(config/settings.yaml の diff_line_budget)。
            token_budget: 実行トークン上限(None は上限なし、Eq.(1) の B_tok 相当)。
        """
        self.diff_line_budget = diff_line_budget
        self.token_budget = token_budget

    def check(
        self,
        proposal: Proposal,
        diff_lines: int,
        evidence: ExecutionEvidence,
        candidate_score: float,
        base_score: float,
        candidate_tokens: int = 0,
        build_ok: bool | None = None,
        smoke_ok: bool | None = None,
    ) -> GateResult:
        """候補の受け入れ判定を行う。

        Python構文確認はここでは new_content に対して直接 compile() で行う
        (Pythonモジュール以外の system_prompt/skill は対象外)。

        Args:
            proposal: 判定対象の候補。
            diff_lines: registry.diff_line_count() の計測値。
            evidence: agent_runner が収集した実行エビデンス。
            candidate_score: 候補ハーネスの evolve 再評価スコア。
            base_score: 現行ハーネスの evolve ベースラインスコア。
            candidate_tokens: 候補実行時の最大タスクトークン(AgentOutput.tokens の最大値)。
            build_ok: docker実行系のイメージビルド成否(None=local実行で判定対象外)。
                [DECISION 2026-11-22] 論文B SG-3 の実行時版: dockerfile/requirements/
                entrypoint の構文・import 相当はビルド成功で検証する。
            smoke_ok: docker実行系のスモークテスト成否(/ping + 有効なenvelope応答)。
                新コードパスが実際に実行されたことの実行時確認(SG-3)。

        Returns:
            GateResult(passed と拒否理由のリスト)。
        """
        reasons: list[str] = []

        # 条件1: Pythonモジュールの構文確認
        # [DECISION 2026-11-22] entrypoint もPythonコードのため構文確認対象に追加。
        if proposal.target_module in ("tool", "verify", "entrypoint"):
            try:
                compile(proposal.new_content, f"<candidate:{proposal.target_module}>", "exec")
            except SyntaxError as exc:
                reasons.append(f"構文エラー: {exc}")

        # 条件1b: docker実行系のビルド成否(足場モジュールの構文/import相当の検証)
        if build_ok is False:
            reasons.append("docker build 失敗: イメージを構築できなかった(構文/import/依存エラー)")

        # 条件1c: docker実行系のスモークテスト成否(新コードパス実行の実行時確認)
        if smoke_ok is False:
            reasons.append("コンテナのスモークテスト失敗: /ping または /invocations が有効応答を返さなかった")

        # 条件2: 実行エビデンス(モジュール種別ごと、設計判断 D-3)
        if proposal.target_module in ("system_prompt", "skill"):
            if not evidence.prompt_included:
                reasons.append(
                    f"{proposal.target_module} モジュールの内容がプロンプトへ組込まれていない"
                )
        else:
            if not evidence.module_imported:
                reasons.append(f"{proposal.target_module} モジュールの import に失敗した")
            if evidence.tool_calls < _MIN_TOOL_CALLS:
                reasons.append(
                    f"{proposal.target_module} モジュールの実行エビデンスがない(tool呼出 {_MIN_TOOL_CALLS} 件未満)"
                )

        # 条件3: 非退行
        if candidate_score < base_score:
            reasons.append(
                f"退行: 候補スコア {candidate_score:.2f} < ベースライン {base_score:.2f}"
            )

        # 条件4: diff行数予算
        if diff_lines > self.diff_line_budget:
            reasons.append(
                f"diff行数予算超過: {diff_lines} 行 > 予算 {self.diff_line_budget} 行"
            )

        # 条件5: トークン予算(Eq.(1) の B_tok)。上限設定時のみ判定する
        if self.token_budget is not None and candidate_tokens > self.token_budget:
            reasons.append(
                f"トークン予算超過: {candidate_tokens} > 上限 {self.token_budget}"
            )

        passed = not reasons
        result = GateResult(passed=passed, reasons=reasons)
        if not passed:
            logger.info("gate rejected candidate (%s): %s", proposal.target_module, reasons)
        return result
