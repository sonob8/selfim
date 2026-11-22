"""コンテナ内エージェント実行の共通ランタイム(固定・Proposer編集外)。

責務:
- ハーネスモジュール(modules/)の読み込み
- プロバイダ選択(env SELFIM_MODEL_PROVIDER: mock / bedrock)
- 呼出記録(CallLog)とプロンプト組成(compose_prompt)
- モジュール配置ディレクトリの解決(コンテナ内 /app とin-processテストの両対応)

[DECISION 2026-11-22] compose_prompt はホスト側 AgentRunner._compose_prompt と
同一規則(system_prompt.strip() + "\\n\\n" + skill.strip())とする。これにより
docker実行系でも「モジュール内容がプロンプトに組込まれた」という実行証拠の
包含判定が local 実行系と同一セマンティクスになる。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol


def _project_root() -> Path:
    """このファイルの位置から /app 相当のルート(モジュール・足場の親)を返す。

    コンテナ内では /app/runtime/agent_runtime.py、in-processテストでは
    src/selfim/harness/runtime/agent_runtime.py に配置されるため、
    親の親ディレクトリをルートとする。
    """
    return Path(__file__).resolve().parent.parent


def modules_dir() -> Path:
    """ハーネスモジュール(modules/)のディレクトリを返す。

    環境変数 SELFIM_MODULES_DIR があればそれを優先する(in-processテスト用)。
    """
    override = os.environ.get("SELFIM_MODULES_DIR")
    if override:
        return Path(override)
    return _project_root() / "modules"


def scaffold_dir() -> Path:
    """足場(scaffold/)のディレクトリを返す(環境変数 SELFIM_SCAFFOLD_DIR 優先)。"""
    override = os.environ.get("SELFIM_SCAFFOLD_DIR")
    if override:
        return Path(override)
    return _project_root() / "scaffold"


def load_modules(directory: Path | None = None) -> dict[str, str]:
    """4コンテンツモジュールの内容を読み込む。

    Args:
        directory: modules ディレクトリ(省略時は modules_dir())。

    Returns:
        sources 辞書(system_prompt / skill / tool_code / verify_code)。
        キー名はホスト側 AgentRunner._load_sources と同一にする
        (mockプロバイダの移植が同一ロジックで済むようにするため)。
    """
    root = directory if directory is not None else modules_dir()

    def _read(name: str) -> str:
        p = root / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    return {
        "system_prompt": _read("system_prompt.txt"),
        "skill": _read("skill.md"),
        "tool_code": _read("tools.py"),
        "verify_code": _read("verify.py"),
    }


def compose_prompt(sources: dict[str, str]) -> str:
    """実行に用いるシステムプロンプトを組み立てる(AgentRunner._compose_prompt と同一規則)。

    Args:
        sources: load_modules の返り値。

    Returns:
        結合済みプロンプト。
    """
    return sources["system_prompt"].strip() + "\n\n" + sources["skill"].strip()


class CallLog:
    """モジュール実行の呼出記録(シリアライズ可能な形式で保持する)。

    ホスト側 selfim.agent_runner.CallLog と同じ記録セマンティクスを持つが、
    JSONへ直接変換できるようエントリを dict で保持する。
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def record(self, module_file: str, function_name: str) -> None:
        """1呼出を記録する。

        Args:
            module_file: モジュールファイル名(tools.py / verify.py)。
            function_name: 呼び出された関数名。
        """
        self.entries.append({"module_file": module_file, "function_name": function_name})

    def count_for(self, module_file: str) -> int:
        """指定モジュールファイルの呼出回数を返す。"""
        return sum(1 for e in self.entries if e["module_file"] == module_file)

    def as_list(self) -> list[dict[str, str]]:
        """InvocationEnvelope.call_log 形式のリストを返す。"""
        return list(self.entries)


class TaskProvider(Protocol):
    """タスク実行プロバイダのインタフェース(mock / bedrock 共通)。"""

    def run(
        self, sources: dict[str, str], task: dict[str, Any], call_log: CallLog
    ) -> tuple[str, dict[str, int]]:
        """1タスクを実行する。

        Args:
            sources: load_modules の返り値。
            task: TaskSpec を dict 化したもの(task_id/prompt/context/reasoning_steps_required)。
            call_log: 呼出記録(実行証拠の源)。

        Returns:
            (出力テキスト, トークン使用量辞書{input_tokens, output_tokens, total_tokens})。
        """
        ...


def get_provider() -> TaskProvider:
    """環境変数 SELFIM_MODEL_PROVIDER に応じたプロバイダを返す。

    Returns:
        mock → MockProvider / bedrock → BedrockProvider。

    Raises:
        ValueError: 未知のプロバイダ名の場合。
    """
    provider = os.environ.get("SELFIM_MODEL_PROVIDER", "mock")
    if provider == "mock":
        from runtime.mock_provider import MockProvider

        return MockProvider()
    if provider == "bedrock":
        from runtime.bedrock_provider import BedrockProvider

        return BedrockProvider()
    raise ValueError(f"未知のプロバイダ: {provider}(有効: mock / bedrock)")


def error_envelope(message: str) -> dict[str, Any]:
    """失敗時の InvocationEnvelope 形式dictを構築する。"""
    return {
        "ok": False,
        "output_text": "",
        "composed_prompt": "",
        "call_log": [],
        "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "error": message,
    }


def parse_payload(payload: Any) -> dict[str, Any]:
    """HTTPペイロードをタスクdictへ正規化する。

    AgentCore の /invocations はバイナリボディを受けるため、ランタイムが
    dict に解決してくれない場合に備え、str/bytes は JSON として解釈する。

    Args:
        payload: エントポイントが受け取った生ペイロード。

    Returns:
        タスクの dict。

    Raises:
        ValueError: JSONとして解釈できない場合。
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes, bytearray)):
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"タスクペイロードがdict形式でない: {type(payload).__name__}")
