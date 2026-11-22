"""Bedrock converse API エミュレータ(moto 拡張)。

moto(AWS API 再現ライブラリ)は bedrock-runtime の invoke_model しか実装して
おらず、strands BedrockModel が使う converse は未対応(moto 5.2.5 時点)。
本モジュールは moto の BedrockRuntimeResponse に converse 実装を
モンキーパッチで追加し、役割別の決定的応答を返す。

これにより「実strands Agent → 実BedrockModel → 実boto3シリアライズ」の
経路をAPIキーなしでテストできる(認証とモデル推論のみをエミュレート)。

使い方:
    from moto import mock_aws
    from tests.bedrock_emulator import install_bedrock_converse_emulator

    install_bedrock_converse_emulator()
    with mock_aws():
        model = build_model("judge", "bedrock")
        ...  # 実boto3クライアント経由でエミュレータ応答が返る

応答ルーティング(system prompt の内容で役割を判定):
- 「ルーブリック採点のジャッジ」→ JudgeOutput 構造化JSON(toolUse)
- 「ハーネス改善の提案者」→ ProposerOutput 構造化JSON(toolUse)
- それ以外 → タスク応答(MockTaskModel と同等のテキスト)
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# テスト用の固定bedrockモデルID(実在しなくてもエミュレータは受理する)
TEST_MODEL_ID = "anthropic.claude-sonnet-4-6-20251014-v1:0"


def _system_text(body: dict[str, Any]) -> str:
    """converse リクエストの system プロンプト全文を返す。"""
    parts = []
    for block in body.get("system", []):
        if isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(parts)


def _first_user_text(body: dict[str, Any]) -> str:
    """最初の user メッセージのテキスト全文を返す。"""
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        for block in message.get("content", []):
            if isinstance(block, dict) and "text" in block:
                return str(block["text"])
    return ""


def _tool_name(body: dict[str, Any]) -> str | None:
    """toolConfig から最初のツール名を返す(構造化出力のツール)。"""
    tools = body.get("toolConfig", {}).get("tools", [])
    if tools:
        return str(tools[0].get("toolSpec", {}).get("name", ""))
    return None


def _judge_payload(body: dict[str, Any]) -> dict[str, Any]:
    """ジャッジ用の構造化応答JSONを返す。

    user プロンプトにエージェント出力が含まれるため、その内容で
    「思考ステップが欠けているか」を決定的に判定する
    (MockJudgeModel と同等のセマンティクス)。
    """
    user_text = _first_user_text(body)
    # タスク側の reasoning_steps_required は user プロンプトに現れないため、
    # エージェント出力中の「ステップn:」行の数で完全性を判定する
    step_lines = [ln for ln in user_text.splitlines() if ln.startswith("ステップ")]
    if len(step_lines) >= 4 and "根拠(事実の引用):" in user_text:
        return {
            "score": 100.0,
            "reason": "全思考ステップと事実引用が確認できた。",
            "missing_points": [],
            "attribution": "model",
        }
    missing: list[str] = []
    if len(step_lines) < 4:
        missing.append(f"思考ステップ欠落: 出力に {4 - len(step_lines)} ステップ分が不足")
    if "根拠(事実の引用):" not in user_text:
        missing.append("事実の引用による根拠付けが不足")
    return {
        "score": 60.0,
        "reason": "思考ステップまたは根拠の明示が不足している。",
        "missing_points": missing,
        "attribution": "harness",
    }


def _proposer_payload(body: dict[str, Any]) -> dict[str, Any]:
    """提案者用の構造化応答JSONを返す(k=1の決定的な候補)。"""
    user_text = _first_user_text(body)
    # SIM全文は system 側に来る。引用すべき既存問題IDが system にあれば引用する
    system = _system_text(body)
    cites: str | None = None
    for line in system.splitlines():
        if line.startswith("[P") and "]" in line:
            cites = line.split("]")[0].lstrip("[")
            break
    name = "required reasoning steps are omitted from the output"
    current_prompt = "あなたは推論タスク"
    proposal = {
        "cites_problem": cites,
        "new_problem_name": None if cites else name,
        "target_module": "system_prompt",
        "what": "system prompt に思考ステップ列挙と事実引用の指示を追加",
        "new_content": (
            f"{current_prompt}\n"
            "必ず各思考ステップを「ステップn:」として明示してください。\n"
            "根拠には事実を引用してください。\n"
        ),
        "predicted": ["legal-001"],
        "rationale": "繰り返し失敗クラスタの不足点に対応する編集",
    }
    return {"proposals": [proposal]}


def _task_response_text(body: dict[str, Any]) -> str:
    """タスクエージェント用のテキスト応答を返す(MockTaskModel相当)。"""
    user_text = _first_user_text(body)
    system = _system_text(body)
    lines = ["結論: 判断を以下に示します。"]
    # system プロンプトの指示マーカーに従って出力構成を変える
    if "ステップn:" in system:
        for i in range(1, 5):
            lines.append(f"ステップ{i}: ステップ{i}を確認した。")
    else:
        lines.append("ステップ1: 要件確認を確認した。")
        lines.append("ステップ2: 該当性検討を確認した。")
    if "事実を引用" in system and "事実:" in user_text:
        fact = next(
            (ln for ln in user_text.splitlines() if "事実" in ln), "事実: (なし)"
        )
        lines.append(f"根拠(事実の引用): {fact.strip()}")
    return "\n".join(lines)


def _converse_response(body: dict[str, Any]) -> dict[str, Any]:
    """converse リクエストから応答レスポンス辞書を組み立てる。

    toolConfig があれば toolUse として構造化JSONを返し、
    なければプレーンテキストを返す。
    """
    system = _system_text(body)
    tool = _tool_name(body)
    if tool is not None:
        if "ルーブリック採点のジャッジ" in system:
            payload: dict[str, Any] = _judge_payload(body)
        elif "ハーネス改善の提案者" in system:
            payload = _proposer_payload(body)
        else:
            payload = {}
        content_block: dict[str, Any] = {
            "toolUse": {
                "toolUseId": "emulated-1",
                "name": tool,
                "input": payload,
            }
        }
    else:
        content_block = {"text": _task_response_text(body)}

    return {
        "output": {"message": {"role": "assistant", "content": [content_block]}},
        "stopReason": "tool_use" if tool is not None else "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        "metrics": {"latencyMs": 5},
    }


def install_bedrock_converse_emulator() -> None:
    """moto の BedrockRuntimeResponse に converse 実装を注入する(冪等)。

    moto 5.2.5 は converse 未実装のため NotImplementedError になる。
    本関数を呼んだ後、mock_aws() コンテキスト内の converse 呼び出しは
    本モジュールの決定的応答を返す。
    """
    from moto.bedrockruntime.responses import BedrockRuntimeResponse

    if getattr(BedrockRuntimeResponse, "_selfim_converse_installed", False):
        return  # 冪等: 二重インストール防止

    def converse(self: Any) -> tuple[int, dict[str, str], str]:
        """エミュレートされた converse アクション。"""
        body = json.loads(self.body)
        response = _converse_response(body)
        logger.debug(
            "emulated converse: model=%s tool=%s",
            body.get("modelId"), _tool_name(body),
        )
        return 200, {"content-type": "application/json"}, json.dumps(response)

    BedrockRuntimeResponse.converse = converse  # type: ignore[method-assign]
    BedrockRuntimeResponse._selfim_converse_installed = True  # type: ignore[attr-defined]
    logger.info("bedrock converse emulator installed (moto extension)")
