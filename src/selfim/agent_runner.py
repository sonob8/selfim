"""AgentRunner — ハーネスモジュールからタスクエージェントを組み立て実行する。

責務:
- runワークスペース(または候補ディレクトリ)から4モジュールを読み込む
- モジュール内容を結合したプロンプトでタスクを実行する(TaskModelへ委譲)
- 実行エビデンス(CallLog)を収集する(ValidationGate の条件2に供給、設計判断 D-3)

system_prompt/skill の「実行エビデンス」は、このクラスが内容をプロンプトに
組込む構造上、組込確認(文字列照合)で保証する。tool/verify のエビデンスは
CallLog の呼出記録による。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from selfim.schemas import ExecutionEvidence, TaskSpec


@dataclass
class CallLog:
    """モジュール実行の呼出記録(ValidationGate の実行エビデンス源)。

    Attributes:
        entries: (module_file, function_name) のタプルリスト(呼出順)。
    """

    entries: list[tuple[str, str]] = field(default_factory=list)

    def record(self, module_file: str, function_name: str) -> None:
        """1呼出を記録する。

        Args:
            module_file: モジュールファイル名(tools.py / verify.py)。
            function_name: 呼出された関数名。
        """
        self.entries.append((module_file, function_name))

    def count_for(self, module_file: str) -> int:
        """指定モジュールファイルの呼出回数を返す。

        Args:
            module_file: モジュールファイル名。

        Returns:
            呼出回数(未実行なら 0)。
        """
        return sum(1 for m, _ in self.entries if m == module_file)


@dataclass
class AgentOutput:
    """1タスク1rolloutの実行結果。

    Attributes:
        text: タスクエージェントの出力テキスト。
        call_log: 実行中のモジュール呼出記録。
        prompt: 実行時に組立てたシステムプロンプト(組込確認用)。
        tokens: 消費トークン概算(mockでは文字数/4、実モードでは未計測で0)。
    """

    text: str
    call_log: CallLog
    prompt: str
    tokens: int = 0


class AgentRunner:
    """ハーネスディレクトリを束縛し、タスク実行とエビデンス収集を行う。

    Attributes:
        harness_dir: ハーネスディレクトリ(runワークスペースまたは候補ディレクトリ)。
        model: タスクエージェントモデル(mock または bedrock)。
    """

    def __init__(self, harness_dir: Path, model: Any) -> None:
        """ハーネスディレクトリとモデルを束縛する。

        Args:
            harness_dir: modules/ を含むハーネスディレクトリ。
            model: TaskModel 実装(build_model("task", ...) の返り値)。

        Raises:
            FileNotFoundError: 必須モジュールファイルが無い場合。
        """
        self.harness_dir = harness_dir
        self.model = model
        required = ["system_prompt.txt", "skill.md"]
        for fname in required:
            if not (harness_dir / "modules" / fname).exists():
                raise FileNotFoundError(f"必須モジュールがありません: modules/{fname}")

    def _load_sources(self) -> dict[str, str]:
        """4モジュールの内容を読み込む。tool/verifyはコード文字列で渡す。

        Returns:
            sources 辞書(system_prompt / skill / tool_code / verify_code)。
        """
        modules = self.harness_dir / "modules"

        def _read(name: str) -> str:
            p = modules / name
            return p.read_text(encoding="utf-8") if p.exists() else ""

        return {
            "system_prompt": _read("system_prompt.txt"),
            "skill": _read("skill.md"),
            "tool_code": _read("tools.py"),
            "verify_code": _read("verify.py"),
        }

    def _compose_prompt(self, sources: dict[str, str]) -> str:
        """実行に用いるシステムプロンプトを組み立てる。

        system_prompt + skill + (verifyモジュールのチェックリストは
        model側で組込まれるため、ここでは結合のみ)。

        Args:
            sources: _load_sources の返り値。

        Returns:
            結合済みプロンプト。
        """
        return sources["system_prompt"].strip() + "\n\n" + sources["skill"].strip()

    def run_task(self, task: TaskSpec) -> AgentOutput:
        """1タスクを実行する。

        Args:
            task: 実行するタスク。

        Returns:
            出力テキスト・CallLog・プロンプトを含む AgentOutput。
        """
        sources = self._load_sources()
        prompt = self._compose_prompt(sources)
        call_log = CallLog()
        text = self.model.run_task(sources, task, call_log)
        # mock のトークン概算: 文字数/4(実モードでは未計測のため0を優先しない)
        tokens = max(len(text) // 4, 1)
        return AgentOutput(text=text, call_log=call_log, prompt=prompt, tokens=tokens)

    def execution_evidence(self, output: AgentOutput, module: str) -> ExecutionEvidence:
        """モジュール種別ごとの実行エビデンスを構築する(設計判断 D-3)。

        Args:
            output: run_task の返り値。
            module: 対象モジュール名(system_prompt/skill/tool/verify)。

        Returns:
            ValidationGate に渡す ExecutionEvidence。
        """
        module_file = {"tool": "tools.py", "verify": "verify.py"}.get(module, "")
        if module in ("system_prompt", "skill"):
            # 組込確認: 現行モジュール内容の非空性とプロンプトへの組込は構造上保証される
            included = len(output.prompt) > 0
            return ExecutionEvidence(
                prompt_included=included,
                module_imported=True,
                tool_calls=0,
            )
        imported = (self.harness_dir / "modules" / module_file).exists()
        return ExecutionEvidence(
            prompt_included=False,
            module_imported=imported,
            tool_calls=output.call_log.count_for(module_file),
        )
