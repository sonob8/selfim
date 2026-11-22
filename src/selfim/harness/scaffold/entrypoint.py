"""エージェント足場のエントポイント(編集可能モジュール・Agent Control 相当)。

契約(この契約を維持する限り、自由に小幅で一般的な改善をしてよい):
- 関数 run_task(task: dict) -> dict を公開すること
- 返り値は InvocationEnvelope 形式の dict:
  {ok, output_text, composed_prompt, call_log, token_usage, error}

固定ランタイム(runtime/)が HTTP サーバとプロバイダ選択を担うため、
ここでは「モジュール読み込み → プロバイダ実行 → envelope 組み立て」の
実行時コード(Agent Control / Action Interface 接続)のみを記述する。

注意: 評価データ・ルーブリックにはアクセスできない(論文B Table III)。
タスクは HTTP ペイロードとして渡されるもののみを扱う。
"""

from __future__ import annotations

from typing import Any

from runtime import agent_runtime


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    """1タスクを実行し、InvocationEnvelope 形式の dict を返す。

    Args:
        task: タスクdict(task_id / prompt / context / reasoning_steps_required)。

    Returns:
        {ok, output_text, composed_prompt, call_log, token_usage, error}。
    """
    sources = agent_runtime.load_modules()
    call_log = agent_runtime.CallLog()
    provider = agent_runtime.get_provider()
    text, usage = provider.run(sources, task, call_log)
    return {
        "ok": True,
        "output_text": text,
        # composed_prompt は実行証拠(prompt_included 判定)の照合対象。
        # AgentRunner._compose_prompt と同一規則で組成すること。
        "composed_prompt": agent_runtime.compose_prompt(sources),
        "call_log": call_log.as_list(),
        "token_usage": usage,
        "error": "",
    }
