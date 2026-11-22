"""AgentRunner — ハーネスモジュールからタスクエージェントを組み立て実行する。

責務:
- runワークスペース(または候補ディレクトリ)から4モジュールを読み込む
- モジュール内容を結合したプロンプトでタスクを実行する(TaskModelへ委譲)
- 実行エビデンス(CallLog)を収集する(ValidationGate の条件2に供給、設計判断 D-3)

system_prompt/skill の「実行エビデンス」は、このクラスが内容をプロンプトに
組込む構造上、組込確認(文字列照合)で保証する。tool/verify のエビデンスは
CallLog の呼出記録による。

[DECISION 2026-11-22] Docker実行系(DockerAgentRunner)との差し替え点として
TaskRunner Protocol を導入した。実行証拠の生成は build_execution_evidence に
集約し、local/docker 両実行系が同一セマンティクスを返すようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from selfim.schemas import ExecutionEvidence, InvocationEnvelope, TaskSpec


class TaskRunner(Protocol):
    """タスク実行器のインタフェース(local: AgentRunner / docker: DockerAgentRunner)。

    Attributes:
        harness_dir: ハーネスディレクトリ(ワークスペースまたは候補ディレクトリ)。
    """

    harness_dir: Path

    def run_task(self, task: TaskSpec) -> "AgentOutput":
        """1タスクを実行し、出力・呼出記録・プロンプトを含む AgentOutput を返す。"""
        ...

    def execution_evidence(self, output: "AgentOutput", module: str) -> ExecutionEvidence:
        """モジュール種別ごとの実行エビデンスを構築する。"""
        ...

    def close(self) -> None:
        """実行器の後始末(コンテナ停止等)。使用後に必ず呼ぶこと。"""
        ...


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
            function_name: 呼び出された関数名。
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

    @classmethod
    def from_envelope(cls, envelope: InvocationEnvelope) -> "CallLog":
        """コンテナから返却された InvocationEnvelope から呼出記録を復元する。

        Args:
            envelope: コンテナ実行の応答ペイロード。

        Returns:
            call_log エントリを反映した CallLog。
        """
        log = cls()
        for entry in envelope.call_log:
            log.record(entry.module_file, entry.function_name)
        return log


@dataclass
class AgentOutput:
    """1タスク1rolloutの実行結果。

    Attributes:
        text: タスクエージェントの出力テキスト。
        call_log: 実行中のモジュール呼出記録。
        prompt: 実行時に組立てたシステムプロンプト(組込確認用)。
        tokens: 消費トークン概算(mockでは文字数/4、実モードでは未計測で0)。
        envelope: docker実行系でのコンテナ応答(local実行では None)。
        error: 実行系の失敗理由(成功時は空文字)。失敗時 text に
            [INVOCATION_ERROR] プレフィックスが付く。
    """

    text: str
    call_log: CallLog
    prompt: str
    tokens: int = 0
    envelope: InvocationEnvelope | None = None
    error: str = ""


def build_execution_evidence(
    output: AgentOutput,
    module: str,
    harness_dir: Path,
    build_ok: bool = True,
    smoke_ok: bool = True,
) -> ExecutionEvidence:
    """モジュール種別ごとの実行エビデンスを構築する(local/docker 共用、設計判断 D-3)。

    証拠テーブル(実行系によらない統一セマンティクス):
    - system_prompt / skill: モジュール内容が実プロンプトへ組込まれたか(文字列包含)。
      docker実行系では出力 prompt として envelope.composed_prompt が使われる。
    - tool / verify: ファイル存在 + CallLog の実呼出回数。
      docker実行系では envelope.call_log を CallLog へ復元したものを使う。
    - entrypoint / dockerfile / requirements: build_ok(イメージビルド成功=
      構文/import 検証通過)と smoke_ok(/invocations が有効なenvelopeを返した=
      編集後のコードパスが実行された)で証明する。

    Args:
        output: run_task の返り値。
        module: 対象モジュール名(7モジュールのいずれか)。
        harness_dir: ハーネスディレクトリ(モジュールファイルの存在確認用)。
        build_ok: docker実行系のビルド成否(local では常に True)。
        smoke_ok: docker実行系のスモークテスト成否(local では常に True)。

    Returns:
        ValidationGate に渡す ExecutionEvidence。
    """
    if module in ("system_prompt", "skill"):
        # [FIX 2026-11-22] 組込確認を「プロンプト非空」から
        # 「モジュール内容の実文字列照合」へ強化した。以前は new_content が
        # 空文字列でも skill 側でプロンプトが非空になるため prompt_included=True
        # になり、空への全置換候補がゲートを通る恐れがあった
        # (最終diff-review 2026-11-22 で指摘、設計判断 D-3 の「内容非空+組込確認」)。
        content = read_module_source(harness_dir, module)
        included = bool(content.strip()) and content.strip() in output.prompt
        return ExecutionEvidence(
            prompt_included=included,
            module_imported=True,
            tool_calls=0,
        )
    if module in ("tool", "verify"):
        module_file = {"tool": "tools.py", "verify": "verify.py"}[module]
        imported = (harness_dir / "modules" / module_file).exists()
        return ExecutionEvidence(
            prompt_included=False,
            module_imported=imported,
            tool_calls=output.call_log.count_for(module_file),
        )
    # entrypoint / dockerfile / requirements(足場モジュール)
    # [DECISION 2026-11-22] 足場はビルド成功(=構文/import相当)とスモークテスト
    # (編集後コードパスの実行)で証拠立てる。local実行系では build_ok/smoke_ok
    # とも True を渡す前提(local では足場編集は評価されない)。
    return ExecutionEvidence(
        prompt_included=False,
        module_imported=build_ok,
        tool_calls=1 if smoke_ok else 0,
    )


def read_module_source(harness_dir: Path, module: str) -> str:
    """コンテンツモジュールファイルの内容を返す(組込確認用)。

    Args:
        harness_dir: ハーネスディレクトリ。
        module: モジュール名(system_prompt/skill)。

    Returns:
        ファイル内容(存在しなければ空文字)。
    """
    fname = {"system_prompt": "system_prompt.txt", "skill": "skill.md"}.get(module, "")
    p = harness_dir / "modules" / fname
    return p.read_text(encoding="utf-8") if p.exists() else ""


class AgentRunner:
    """ハーネスディレクトリを束縛し、タスク実行とエビデンス収集を行う(local実行系)。

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
        """モジュール種別ごとの実行エビデンスを構築する(共通関数へ委譲)。

        Args:
            output: run_task の返り値。
            module: 対象モジュール名(7モジュールのいずれか)。

        Returns:
            ValidationGate に渡す ExecutionEvidence。
        """
        return build_execution_evidence(output, module, self.harness_dir)

    def close(self) -> None:
        """ローカル実行は後始末不要(TaskRunner インタフェース整合のため存在する)。"""

    def read_module_source(self, module: str) -> str:
        """モジュールファイルの内容を返す(組込確認用)。"""
        return read_module_source(self.harness_dir, module)
