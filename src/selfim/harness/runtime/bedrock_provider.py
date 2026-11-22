"""コンテナ内bedrockプロバイダ(固定・strands Agent + BedrockModel)。

src/selfim/models.py の StrandsTaskModel.run_task のロジック移植。
[DECISION 2026-11-22] 候補コード(tools/verify)の exec をコンテナ内で行う
(論文B Table III:ループプロセスは候補コードを実行しない)。

注意: この経路は実AWSクレデンシャルがないとテストできない(プロジェクトの
テストはmockで網羅し、bedrock接続性は実環境でのスモーク確認とする)。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class BedrockProvider:
    """strands Agent(BedrockModel)によるタスク実行プロバイダ(bedrockモード)。"""

    def _build_model(self) -> Any:
        """env から BedrockModel を組み立てる(遅延import: strandsはコンテナ内依存)。"""
        from strands.models import BedrockModel

        model_id = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6-20251014-v1:0")
        region = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-west-2"))
        # streaming=False はモック/エミュレータとの互換のため(ホスト側 _StrandsBase と同一判断)
        return BedrockModel(model_id=model_id, region_name=region, streaming=False)

    def run(
        self, sources: dict[str, str], task: dict[str, Any], call_log: Any
    ) -> tuple[str, dict[str, int]]:
        """1タスクを実行し(出力テキスト, トークン使用量)を返す。

        Args:
            sources: モジュール名 → 内容。
            task: タスクdict。
            call_log: runtime.agent_runtime.CallLog。

        Returns:
            (出力テキスト, strands の usage メタデータ由来のトークン使用量)。
        """
        from strands import Agent

        model = self._build_model()
        system = sources.get("system_prompt", "") + "\n" + sources.get("skill", "")

        # verify モジュールの実行を CallLog に記録する
        # (ホスト側 StrandsTaskModel の [FIX 2026-11-22] と同一の意図: verify への
        #  Proposal が実行エビデンスなしでゲート拒否される問題の移植版対策)。
        if sources.get("verify_code"):
            try:
                namespace: dict[str, Any] = {}
                exec(compile(sources["verify_code"], "<verify>", "exec"), namespace)  # noqa: S102
                build = namespace.get("build_checklist")
                if callable(build):
                    items = build(str(task.get("task_id", "")))
                    items_text = "\n".join(f"- {x}" for x in items)
                    system += f"\n\n# 完了前チェックリスト\n{items_text}"
                    call_log.record("verify.py", "build_checklist")
            except Exception as exc:  # noqa: BLE001
                logger.warning("verify module execution failed: %s", exc)

        tools = self._load_tools(sources.get("tool_code", ""))
        agent = Agent(model=model, system_prompt=system, tools=tools, callback_handler=None)
        user = f"{task.get('prompt', '')}\n\n{task.get('context', '')}"
        result = agent(user)

        # toolUse の実際の発生を記録する(ホスト側 [FIX 2026-11-22] と同一:登録数でなく実呼出)
        content_blocks = result.message.get("content", []) if isinstance(result.message, dict) else []
        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict):
                if "text" in block:
                    texts.append(str(block["text"]))
                if "toolUse" in block:
                    call_log.record("tools.py", str(block["toolUse"].get("name", "tool")))
        text = "\n".join(texts) if texts else str(result)
        return text, self._extract_usage(result)

    @staticmethod
    def _load_tools(tool_code: str) -> list[Any]:
        """tool モジュールから strands @tool 関数を収集する(ホスト側と同一規則)。"""
        if not tool_code:
            return []
        namespace: dict[str, Any] = {}
        try:
            exec(compile(tool_code, "<tools>", "exec"), namespace)  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool module import failed: %s", exc)
            return []
        return [v for v in namespace.values() if callable(v) and hasattr(v, "_tool_spec")]

    @staticmethod
    def _extract_usage(result: Any) -> dict[str, int]:
        """strands の結果からトークン使用量を抽出する(取得不能時は0)。"""
        try:
            metrics = getattr(result, "metrics", None)
            usage = getattr(metrics, "accumulated_usage", None) if metrics else None
            if isinstance(usage, dict):
                return {
                    "input_tokens": int(usage.get("inputTokens", 0)),
                    "output_tokens": int(usage.get("outputTokens", 0)),
                    "total_tokens": int(usage.get("totalTokens", 0)),
                }
        except Exception as exc:  # noqa: BLE001 - usage取得はベストエフォート
            logger.warning("usage extraction failed: %s", exc)
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
