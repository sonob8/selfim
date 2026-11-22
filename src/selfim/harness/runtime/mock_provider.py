"""コンテナ内mockプロバイダ(固定・決定的・キー不要)。

src/selfim/models.py の MockTaskModel.run_task のロジック移植。
[DECISION 2026-11-22] ループプロセスが候補コードを exec() するのは論文B Table III
(評価器の隔離)に反するため、tools/verify の exec はコンテナ内(this file 経由)で
実行する。マーカー定数は models.py と重複定義になるが、コンテナ内コードは
ホスト側 selfim パッケージに依存させてはならないため許容する(変更時は両側修正)。

マーカー駆動の挙動(設計判断 D-5):
- MARKER_ALL_STEPS があれば全思考ステップを出力、なければ先頭2つのみ
- MARKER_FACT_CITE があれば context の事実を引用
- MARKER_COUNTER があれば反対解釈セクションを出力
- verify モジュールの build_checklist 呼出結果を「検証チェック」として出力
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ホスト側 models.py の MARKER_* と同一文言(同期必須)。
MARKER_ALL_STEPS = "ステップn:"
MARKER_FACT_CITE = "根拠には事実を引用"
MARKER_COUNTER = "反対解釈を検討"


class MockProvider:
    """ハーネス内容に敏感な決定的タスクプロバイダ(mockモード)。"""

    def run(
        self, sources: dict[str, str], task: dict[str, Any], call_log: Any
    ) -> tuple[str, dict[str, int]]:
        """1タスクを実行し(出力テキスト, トークン使用量)を返す。

        Args:
            sources: モジュール名 → 内容(system_prompt/skill/tool_code/verify_code)。
            task: タスクdict(task_id/prompt/context/reasoning_steps_required)。
            call_log: runtime.agent_runtime.CallLog(実行証拠の源)。

        Returns:
            (出力テキスト, {input_tokens:0, output_tokens:0, total_tokens:0})。
            mockは実モデルを呼ばないためトークン使用量は0。
        """
        prompt = sources.get("system_prompt", "")
        skill = sources.get("skill", "")
        combined = prompt + "\n" + skill

        task_id = str(task.get("task_id", ""))
        steps = list(task.get("reasoning_steps_required", []))
        context = str(task.get("context", ""))

        lines: list[str] = [f"結論: {task_id} に関する判断を以下に示します。"]

        # 思考ステップ(列挙指示の有無で全件/先頭2件が変わる)
        shown = steps if MARKER_ALL_STEPS in combined else steps[:2]
        for i, step in enumerate(shown, start=1):
            lines.append(f"ステップ{i}: {step} を確認した。")

        if MARKER_FACT_CITE in combined and context:
            fact_line = next(
                (ln for ln in context.splitlines() if ln.strip().startswith("事実")),
                context.strip().splitlines()[0] if context.strip() else "",
            )
            lines.append(f"根拠(事実の引用): {fact_line.strip()}")

        if MARKER_COUNTER in combined:
            lines.append("反対解釈の検討: 結論に反する解釈と制限事由を検討した結果、採用しない。")

        # tool モジュール: outline 関数を呼出し CallLog に記録する
        tool_src = sources.get("tool_code", "")
        if tool_src:
            try:
                namespace: dict[str, Any] = {}
                exec(compile(tool_src, "<tools>", "exec"), namespace)  # noqa: S102 - 候補ハーネス実行用(コンテナ内)
                outline = namespace.get("outline")
                if callable(outline):
                    outline(steps)
                    call_log.record("tools.py", "outline")
            except Exception as exc:  # noqa: BLE001 - 候補コードは任意に壊れ得る
                logger.warning("tool module execution failed: %s", exc)

        # verify モジュールのチェックリスト(実呼出により CallLog に記録される)
        checklist: list[str] = []
        verify_src = sources.get("verify_code", "")
        if verify_src:
            try:
                namespace_v: dict[str, Any] = {}
                exec(compile(verify_src, "<verify>", "exec"), namespace_v)  # noqa: S102
                build = namespace_v.get("build_checklist")
                if callable(build):
                    checklist = list(build(task_id))
                    call_log.record("verify.py", "build_checklist")
            except Exception as exc:  # noqa: BLE001
                logger.warning("verify module execution failed: %s", exc)
        for item in checklist:
            lines.append(f"検証チェック: {item}")

        return "\n".join(lines), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
