"""AgentCore互換HTTPサーバの固定アダプタ(Proposer編集外)。

責務:
- bedrock_agentcore.runtime.BedrockAgentCoreApp で /invocations, /ping を 8080 番で待受け
- scaffold/entrypoint.py(編集可能)の run_task を呼び、結果を InvocationEnvelope 形式に正規化
- エントポイントの例外を ok=False のエラーenvelopeへ変換(HTTPサーバを死なせない)

[DECISION 2026-11-22] bedrock_agentcore の import は create_app()/main 内に限定する。
これによりホスト側テストは同パッケージをインストールせずに invoke の
正規化・例外変換ロジックを検証できる(コンテナ内でのみ重依存が解決される)。
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from runtime.agent_runtime import error_envelope, parse_payload, scaffold_dir

logger = logging.getLogger(__name__)

# InvocationEnvelope の全キーと既定値(正規化用)。
_ENVELOPE_DEFAULTS: dict[str, Any] = {
    "ok": True,
    "output_text": "",
    "composed_prompt": "",
    "call_log": [],
    "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    "error": "",
}


def _load_entrypoint() -> Any:
    """scaffold/entrypoint.py を動的ロードして返す(モジュールオブジェクト)。

    scaffold は Pythonパッケージではなくディレクトリのため importlib で
    ファイルから直接ロードする。Proposer が entrypoint.py を全置換しても
    このローダ自体は影響を受けない。
    """
    path = scaffold_dir() / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("scaffold_entrypoint", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"entrypoint をロードできません: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize(result: Any) -> dict[str, Any]:
    """エントポイントの返り値をenvelope形式へ正規化する。

    エントポイントが部分的なdictしか返さない場合でも全キーを保証する
    (ホスト側 Pydantic InvocationEnvelope での検証失敗を防ぐ)。
    """
    if not isinstance(result, dict):
        return error_envelope(f"entrypoint の返り値がdictでない: {type(result).__name__}")
    normalized = dict(_ENVELOPE_DEFAULTS)
    normalized.update(result)
    return normalized


def invoke(payload: Any) -> dict[str, Any]:
    """1回の /invocations 処理(例外を捕捉しエラーenvelopeへ変換する)。

    Args:
        payload: ランタイムが解決したリクエストボディ(dict または JSON文字列)。

    Returns:
        InvocationEnvelope 形式の dict。
    """
    try:
        task = parse_payload(payload)
        entrypoint = _load_entrypoint()
        return _normalize(entrypoint.run_task(task))
    except Exception as exc:  # noqa: BLE001 - サーバ存続のため全例外を捕捉する
        logger.exception("invocation failed")
        return error_envelope(f"{type(exc).__name__}: {exc}")


def create_app() -> Any:
    """BedrockAgentCoreApp を構築し、invoke をエントポイントとして登録する。

    Returns:
        登録済みの BedrockAgentCoreApp(bedrock_agentcore 依存はここで初めて解決)。
    """
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()
    app.entrypoint(invoke)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run()
