"""models — プロバイダファクトリ(設計判断 D-4/D-5)。

役割(task/judge/proposer/selector)ごとに LLM クライアントを組み立てる。
- mock: 決定的な応答を返す実装(APIキー不要、テストとデモ用)。
- bedrock: strands Agent(BedrockModel)による実装(構造化出力はPydanticモデル)。

ロジック(SIM更新・ゲート・スクリーニング規則)の正しさはmockで検証し、
bedrock経路の接続性はスモークテストで確認する。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from selfim.schemas import (
    EvaluationResult,
    FailureCluster,
    Proposal,
    Rubric,
    SelectionResult,
    SimRecord,
    TaskSpec,
)

logger = logging.getLogger(__name__)

ROLES = ("task", "judge", "proposer", "selector")
PROVIDERS = ("mock", "bedrock")


# ---------------------------------------------------------------------------
# 抽象インタフェース(役割ごと)
# ---------------------------------------------------------------------------


class TaskModel(ABC):
    """タスクエージェントのモデル。harness ソースを受けてタスク出力を返す。"""

    role = "task"
    provider: str = "abstract"

    @abstractmethod
    def run_task(self, sources: dict[str, str], task: TaskSpec, call_log: Any) -> str:
        """1タスクを実行し出力テキストを返す。

        Args:
            sources: モジュール名 → 内容(system_prompt/skill/tool/verifyのソース等)。
            task: 実行するタスク。
            call_log: 実行記録(モジュール実行エビデンスの源)。実装は
                verify/tool モジュールを実行した際に record() で記録すること
                (ValidationGate の実行エビデンス要件、設計判断 D-3)。

        Returns:
            タスク出力テキスト。
        """


class JudgeModel(ABC):
    """LLM-as-a-Judge のモデル。ルーブリックに従い採点する。"""

    role = "judge"
    provider: str = "abstract"

    @abstractmethod
    def judge(self, task: TaskSpec, rubric: Rubric, output_text: str) -> EvaluationResult:
        """出力をルーブリック採点する。

        Args:
            task: 採点対象のタスク。
            rubric: 採点ルーブリック(ユーザー提供)。
            output_text: タスクエージェントの出力。

        Returns:
            数値スコアと理由・不足点を含む EvaluationResult。
        """


class ProposerModel(ABC):
    """改善候補を提案するモデル。"""

    role = "proposer"
    provider: str = "abstract"

    @abstractmethod
    def propose(
        self,
        harness_sources: dict[str, str],
        clusters: list[FailureCluster],
        sim_text: str,
        task_ids: list[str],
        k: int,
    ) -> list[Proposal]:
        """失敗クラスタとSIM全文から k 件の候補を生成する。

        Args:
            harness_sources: 現行ハーネスのモジュール名 → 内容。
            clusters: 繰り返し失敗クラスタ(count==1 は含まれない)。
            sim_text: SIMの全エントリテキスト(render_for_prompt の出力)。
            task_ids: evolveセットの全タスクID(予測の対象)。
            k: 生成する候補数。

        Returns:
            Proposal のリスト(各候補は1モジュールのみ編集)。
        """


class SelectorModel(ABC):
    """スクリーニングと選択を支援するモデル。"""

    role = "selector"
    provider: str = "abstract"

    @abstractmethod
    def advisory_select(
        self,
        base_score: float,
        candidates: list[dict[str, Any]],
        sim_text: str,
    ) -> SelectionResult:
        """評価済み候補の中から採択候補を判断する(選択のLLM判断部分)。

        Args:
            base_score: 現行ハーネスのスコア。
            candidates: {candidate_id, proposal概要, score, diff_lines, gate} の辞書リスト。
            sim_text: SIM全文。

        Returns:
            SelectionResult(採択IDまたは None)。
        """


# ---------------------------------------------------------------------------
# mock 実装(決定的・キー不要)
# ---------------------------------------------------------------------------

# mock タスクモデルが system_prompt/skill から読み取る指示マーカー。
# MockProposerModel がこれらの文を編集として追加することで、mockループでも
# 「編集 → 出力改善 → スコア向上」が観測できる(設計判断 D-5)。
MARKER_ALL_STEPS = "ステップn:"
MARKER_FACT_CITE = "根拠には事実を引用"
MARKER_COUNTER = "反対解釈を検討"


class MockTaskModel(TaskModel):
    """ハーネス内容に敏感な決定的タスクモデル(mockモード)。

    system_prompt/skill の指示マーカーの有無で出力の構成要素が変わる:
    - MARKER_ALL_STEPS があれば全思考ステップを出力、なければ先頭2つのみ
    - MARKER_FACT_CITE があれば context の事実を引用
    - MARKER_COUNTER があれば反対解釈セクションを出力
    - verify モジュールの build_checklist 呼出結果を「検証チェック」として出力
    """

    provider = "mock"

    def run_task(self, sources: dict[str, str], task: TaskSpec, call_log: Any) -> str:
        prompt = sources.get("system_prompt", "")
        skill = sources.get("skill", "")
        combined = prompt + "\n" + skill

        lines: list[str] = [f"結論: {task.task_id} に関する判断を以下に示します。"]

        # 思考ステップ(列挙指示の有無で全件/先頭2件が変わる)
        steps = task.reasoning_steps_required
        if MARKER_ALL_STEPS in combined:
            shown = steps
        else:
            shown = steps[:2]
        for i, step in enumerate(shown, start=1):
            lines.append(f"ステップ{i}: {step} を確認した。")

        if MARKER_FACT_CITE in combined and task.context:
            fact_line = next(
                (ln for ln in task.context.splitlines() if ln.strip().startswith("事実")),
                task.context.strip().splitlines()[0] if task.context.strip() else "",
            )
            lines.append(f"根拠(事実の引用): {fact_line.strip()}")

        if MARKER_COUNTER in combined:
            lines.append("反対解釈の検討: 結論に反する解釈と制限事由を検討した結果、採用しない。")

        # tool モジュール: outline 関数を呼出し CallLog に記録する
        tool_src = sources.get("tool_code", "")
        if tool_src:
            try:
                namespace: dict[str, Any] = {}
                exec(compile(tool_src, "<tools>", "exec"), namespace)  # noqa: S102 - 候補ハーネス実行用
                outline = namespace.get("outline")
                if callable(outline):
                    outline(list(task.reasoning_steps_required))
                    call_log.record("tools.py", "outline")
            except Exception as exc:  # noqa: BLE001 - 候補コードは任意に壊れ得る
                logger.warning("tool module execution failed: %s", exc)

        # verify モジュールのチェックリスト(実呼出により CallLog に記録される)
        checklist: list[str] = []
        verify_src = sources.get("verify_code", "")
        if verify_src:
            try:
                namespace: dict[str, Any] = {}
                exec(compile(verify_src, "<verify>", "exec"), namespace)  # noqa: S102
                build = namespace.get("build_checklist")
                if callable(build):
                    checklist = list(build(task.task_id))
                    call_log.record("verify.py", "build_checklist")
            except Exception as exc:  # noqa: BLE001 - 候補コードは任意に壊れ得る
                logger.warning("verify module execution failed: %s", exc)
        for item in checklist:
            lines.append(f"検証チェック: {item}")

        return "\n".join(lines)


class MockJudgeModel(JudgeModel):
    """ルーブリック基準名のキーワードで決定的採点するジャッジ(mockモード)。

    基準名に含まれるキーワードで素点を決める:
    - 「結論」: 出力に結論行があれば 100 / なければ 40
    - 「思考ステップ」: 必須ステップの明示率 × 100
    - 「根拠」: 事実引用があれば 100 / なければ 30
    - 「反対解釈」「公平性」: 反対解釈セクションがあれば 100 / なければ 0
    - 上記以外の基準: 中立値 60
    """

    provider = "mock"

    def judge(self, task: TaskSpec, rubric: Rubric, output_text: str) -> EvaluationResult:
        missing_points: list[str] = []
        total = 0.0
        for criterion in rubric.criteria:
            name = criterion.name
            if "結論" in name:
                score_i = 100.0 if "結論:" in output_text else 40.0
            elif "思考ステップ" in name:
                shown = [s for s in task.reasoning_steps_required if s in output_text]
                for s in task.reasoning_steps_required:
                    if s not in output_text:
                        missing_points.append(f"思考ステップ欠落: {s}")
                score_i = 100.0 * len(shown) / len(task.reasoning_steps_required) if task.reasoning_steps_required else 100.0
            elif "根拠" in name:
                has_fact = "根拠(事実の引用):" in output_text
                if not has_fact:
                    missing_points.append("事実の引用による根拠付けが不足")
                score_i = 100.0 if has_fact else 30.0
            elif ("反対解釈" in name) or ("公平性" in name):
                has_counter = "反対解釈の検討:" in output_text
                if not has_counter:
                    missing_points.append("反対解釈・公平性の検討が不足")
                score_i = 100.0 if has_counter else 0.0
            else:
                score_i = 60.0
            total += criterion.weight * score_i

        attribution = "harness" if missing_points else "model"
        return EvaluationResult(
            task_id=task.task_id,
            score=round(total, 2),
            reason=f"ルーブリック基準 {len(rubric.criteria)} 項を決定的採点した。",
            missing_points=missing_points,
            attribution=attribution,  # type: ignore[arg-type]
            output_text=output_text,
        )


class MockProposerModel(ProposerModel):
    """失敗クラスタの種類に応じた決定的編集を提案する(mockモード)。

    最初のクラスタに対応する指示マーカーを system_prompt に追加する編集を返す。
    k>=2 の場合、2つ目の候補にタスクID直書きのヒントを含める
    (Selector のスクリーニング規則「タスク固有ヒント拒否」の検証に使われる)。
    """

    provider = "mock"

    def propose(
        self,
        harness_sources: dict[str, str],
        clusters: list[FailureCluster],
        sim_text: str,
        task_ids: list[str],
        k: int,
    ) -> list[Proposal]:
        if not clusters:
            return []
        cluster = clusters[0]
        missing_text = "\n".join(cluster.sample_missing_points)

        # 根原因キー: SIMに同名エントリがあれば引用、なければ新規命名
        cited = self._find_sim_problem(sim_text, cluster.root_cause_hypothesis)

        # クラスタの不足内容から編集方針を決める
        additions: list[str] = []
        if "思考ステップ欠落" in missing_text:
            additions.append(f"必ず各思考ステップを「{MARKER_ALL_STEPS}」として明示してください。")
        if "事実の引用" in missing_text:
            additions.append(f"{MARKER_FACT_CITE}してください。")
        if "反対解釈" in missing_text or "公平性" in missing_text:
            additions.append(f"{MARKER_COUNTER}してください。")
        if not additions:
            additions.append("出力の構成を明確にしてください。")

        current = harness_sources.get("system_prompt", "")
        clean = current + "\n" + "\n".join(additions)

        proposals: list[Proposal] = []
        base_kwargs: dict[str, Any] = dict(
            target_module="system_prompt",
            what=f"system prompt に指示を追加: {additions[0]}",
            predicted=task_ids[:2] if task_ids else ["t1"],
            rationale=cluster.root_cause_hypothesis,
        )
        if cited:
            proposals.append(Proposal(cites_problem=cited, new_content=clean, **base_kwargs))
        else:
            proposals.append(Proposal(new_problem_name=cluster.root_cause_hypothesis, new_content=clean, **base_kwargs))

        # k>=2: スクリーニングで落ちるべきタスク固有ヒント候補を意図的に含める
        if k >= 2:
            hint = clean + f"\nこの指示はタスク {task_ids[0] if task_ids else 't1'} 専用です。\n"
            kwargs2 = dict(base_kwargs)
            kwargs2["what"] = "タスク固有のヒントを追加(スクリーニングされるべき)"
            if cited:
                proposals.append(Proposal(cites_problem=cited, new_content=hint, **kwargs2))
            else:
                proposals.append(Proposal(new_problem_name=cluster.root_cause_hypothesis, new_content=hint, **kwargs2))

        return proposals[:k]

    @staticmethod
    def _find_sim_problem(sim_text: str, hypothesis: str) -> str | None:
        """SIMテキスト内に同根原因のエントリがあればそのIDを返す。"""
        for line in sim_text.splitlines():
            if line.startswith("[P") and hypothesis in line:
                return line.split("]")[0].lstrip("[")
        return None


class MockSelectorModel(SelectorModel):
    """決定的な選択助言を返す(mockモード)。

    実際のスクリーニング規則は Selector 側の決定的コードで行い、
    このクラスは「最も高いスコアのゲート通過候補」を助言するだけの役割分担とする。
    """

    provider = "mock"

    def advisory_select(
        self, base_score: float, candidates: list[dict[str, Any]], sim_text: str
    ) -> SelectionResult:
        passed = [c for c in candidates if c.get("gate_passed")]
        if not passed:
            return SelectionResult(accepted_candidate_id=None, reason="ゲート通過候補なし")
        best = max(passed, key=lambda c: (c["score"], -c["diff_lines"]))
        if best["score"] <= base_score:
            return SelectionResult(
                accepted_candidate_id=None,
                reason=f"最良候補 {best['score']:.2f} がベースライン {base_score:.2f} 以下のため不採択",
            )
        return SelectionResult(
            accepted_candidate_id=best["candidate_id"],
            reason=f"スコア {base_score:.2f} -> {best['score']:.2f}",
        )


# ---------------------------------------------------------------------------
# bedrock 実装(strands Agent ラップ)
# ---------------------------------------------------------------------------


def _require_strands() -> Any:
    """bedrock 用に strands/boto3 を遅延importする。"""
    try:
        import boto3  # noqa: F401
        from strands import Agent
        from strands.models import BedrockModel
    except ImportError as exc:
        raise RuntimeError(
            "bedrock プロバイダには strands-agents と boto3 が必要です。"
            "`uv add strands-agents` を実行し、AWS認証情報を設定してください。"
        ) from exc
    return Agent, BedrockModel


class _StrandsBase:
    """bedrock モデル共通の遅延初期化。"""

    provider = "bedrock"

    def __init__(self, model_id: str, region: str) -> None:
        """モデルIDとリージョンを保持する(接続は初回呼出時)。"""
        self.model_id = model_id
        self.region = region
        self._agent_cls: Any = None
        self._model: Any = None

    def _ensure(self) -> Any:
        """strands Agent を遅延構築する。"""
        if self._agent_cls is None:
            Agent, BedrockModel = _require_strands()  # type: ignore[misc]
            self._agent_cls = Agent
            self._model = BedrockModel(model_id=self.model_id, region_name=self.region)
        return self._agent_cls


class StrandsJudgeModel(_StrandsBase, JudgeModel):
    """strands Agent の構造化出力でルーブリック採点するジャッジ。"""

    def judge(self, task: TaskSpec, rubric: Rubric, output_text: str) -> EvaluationResult:
        from pydantic import BaseModel as _BM

        Agent = self._ensure()

        class JudgeOutput(_BM):
            """ジャッジの構造化出力スキーマ。"""

            score: float
            reason: str
            missing_points: list[str]
            attribution: str

        criteria_text = "\n".join(
            f"- {c.name} (weight={c.weight}): {c.description}" for c in rubric.criteria
        )
        system = (
            "あなたはルーブリック採点のジャッジです。0-100の数値スコアと、"
            "評価理由・不足点(missing_points)を出力してください。"
            "失敗原因が手順・出力形式の指示の欠如なら attribution=harness、"
            "モデルの知識・判断力の欠如なら attribution=model を出力してください。\n"
            f"採点基準:\n{criteria_text}\n{rubric.judge_instructions}"
        )
        user = f"# タスク\n{task.prompt}\n{task.context}\n\n# エージェントの出力\n{output_text}"
        agent = Agent(model=self._model, system_prompt=system, callback_handler=None)
        result = agent(user, structured_output_model=JudgeOutput)
        out: JudgeOutput = result.structured_output
        return EvaluationResult(
            task_id=task.task_id,
            score=float(out.score),
            reason=out.reason,
            missing_points=list(out.missing_points),
            attribution=out.attribution,  # type: ignore[arg-type]
            output_text=output_text,
        )


def build_proposer_prompt(
    harness_sources: dict[str, str],
    clusters: list[FailureCluster],
    sim_text: str,
    task_ids: list[str],
    k: int,
) -> tuple[str, str]:
    """Proposer用の(system prompt, user prompt)を組み立てる(テスト可能な純関数)。

    system prompt に論文A D.2 の8原則とSIM全文を埋め込む
    (論文A §3.2: ProposerはSIM全文をsystem promptで受け取る)。

    Args:
        harness_sources: モジュール名 → 内容。
        clusters: 失敗クラスタ。
        sim_text: render_for_prompt の出力。
        task_ids: タスクID一覧。
        k: 候補数。

    Returns:
        (system_prompt, user_prompt)。
    """
    system = (
        "あなたはハーネス改善の提案者です。SIM(改善履歴)全文を参照し、"
        "以下の原則に従ってください:\n"
        "1. エントリは過去の経験として柔軟に扱う\n"
        "2. エントリのドメイン・ベンチマーク・モデルを確認する\n"
        "3. 既存診断で現在の失敗を説明できるならそれを出発点にする\n"
        "4. 根拠を再利用し、設定変更後は編集を再評価する\n"
        "5. 新エビデンスがあれば編集を改訂・置換・上書きする\n"
        "6. テスト済みの方向は新エビデンスがある時だけ再訪する\n"
        "7. 既存診断で説明できない失敗は新しい問題エントリとして登録する\n"
        "8. 似た設定ではタスク固有エビデンスを重視し、両方が変われば軽視する\n"
        "各候補は1モジュールのみ編集し、既存根原因の引用(cites_problem)か"
        "新規根原因の命名(new_problem_name)のどちらか一方を必ず含め、"
        "修正される予測タスク(predicted)を必ず挙げてください。\n"
        f"\n# SIM(全文)\n{sim_text}"
    )
    sources_text = "\n\n".join(f"## {name}\n{content}" for name, content in harness_sources.items())
    clusters_text = "\n".join(
        f"- {c.root_cause_hypothesis} (tasks={c.task_ids}, count={c.count}, "
        f"missing={c.sample_missing_points})"
        for c in clusters
    )
    user = (
        f"# 現行ハーネス\n{sources_text}\n\n# 失敗クラスタ\n{clusters_text}\n\n"
        f"# タスクID一覧\n{task_ids}\n\n{k}件の候補を提案してください。"
    )
    return system, user


class StrandsProposerModel(_StrandsBase, ProposerModel):
    """strands Agent の構造化出力で改善候補を提案する(SIM全文+D.2原則を含む)。"""

    def propose(
        self,
        harness_sources: dict[str, str],
        clusters: list[FailureCluster],
        sim_text: str,
        task_ids: list[str],
        k: int,
    ) -> list[Proposal]:
        from pydantic import BaseModel as _BM, Field as _Field

        Agent = self._ensure()

        class ProposalOut(_BM):
            """単一候補の構造化出力スキーマ。"""

            cites_problem: str | None = None
            new_problem_name: str | None = None
            target_module: str
            what: str
            new_content: str
            predicted: list[str] = _Field(min_length=1)
            rationale: str = ""

        class ProposerOutput(_BM):
            """候補リストの構造化出力スキーマ。"""

            proposals: list[ProposalOut]

        system, user = build_proposer_prompt(harness_sources, clusters, sim_text, task_ids, k)
        agent = Agent(model=self._model, system_prompt=system, callback_handler=None)
        result = agent(user, structured_output_model=ProposerOutput)
        out: ProposerOutput = result.structured_output
        proposals: list[Proposal] = []
        for p in out.proposals[:k]:
            proposals.append(
                Proposal(
                    cites_problem=p.cites_problem,
                    new_problem_name=p.new_problem_name,
                    target_module=p.target_module,  # type: ignore[arg-type]
                    what=p.what,
                    new_content=p.new_content,
                    predicted=p.predicted,
                    rationale=p.rationale,
                )
            )
        return proposals


class StrandsSelectorModel(_StrandsBase, SelectorModel):
    """strands Agent で採択判断するセレクタ(決定的規則の補助として使用)。"""

    def advisory_select(
        self, base_score: float, candidates: list[dict[str, Any]], sim_text: str
    ) -> SelectionResult:
        from pydantic import BaseModel as _BM

        Agent = self._ensure()

        class SelectionOut(_BM):
            """選択の構造化出力スキーマ。"""

            accepted_candidate_id: str | None
            reason: str

        lines = "\n".join(
            f"- {c['candidate_id']}: score={c['score']:.2f}, diff_lines={c['diff_lines']}, "
            f"what={c['what']}"
            for c in candidates
        )
        system = (
            "あなたはハーネス改善候補の選択者です。スコア向上・複雑度・ドメイン特化度・"
            "退行リスクを考慮し、1件を採択するか全不採択を判断します。"
            "SIMの過去試行で失敗した方向は新エビデンスがない限り避けてください。\n"
            f"# SIM(全文)\n{sim_text}"
        )
        user = f"ベースライン: {base_score:.2f}\n# 候補(ゲート通過のみ)\n{lines}"
        agent = Agent(model=self._model, system_prompt=system, callback_handler=None)
        result = agent(user, structured_output_model=SelectionOut)
        out: SelectionOut = result.structured_output
        return SelectionResult(
            accepted_candidate_id=out.accepted_candidate_id, reason=out.reason
        )


class StrandsTaskModel(_StrandsBase, TaskModel):
    """strands Agent による実タスク実行(bedrockモード)。

    注意: tools モジュールは strands @tool デコレータ付き関数のみ登録する
    (装飾なし関数は無視)。シードの plain 関数はmockモード専用である。
    """

    def run_task(self, sources: dict[str, str], task: TaskSpec, call_log: Any) -> str:
        Agent = self._ensure()
        system = sources.get("system_prompt", "") + "\n" + sources.get("skill", "")
        if sources.get("verify_code"):
            try:
                namespace: dict[str, Any] = {}
                exec(compile(sources["verify_code"], "<verify>", "exec"), namespace)  # noqa: S102
                build = namespace.get("build_checklist")
                if callable(build):
                    items = "\n".join(f"- {x}" for x in build(task.task_id))
                    system += f"\n\n# 完了前チェックリスト\n{items}"
            except Exception as exc:  # noqa: BLE001
                logger.warning("verify module execution failed: %s", exc)

        tools = self._load_tools(sources.get("tool_code", ""))
        for _ in tools:
            call_log.record("tools.py", "strands_tool")
        agent = Agent(model=self._model, system_prompt=system, tools=tools, callback_handler=None)
        user = f"{task.prompt}\n\n{task.context}"
        result = agent(user)
        # result.message は角色付きメッセージ辞書。テキスト部分を連結して返す。
        content = result.message.get("content", []) if isinstance(result.message, dict) else []
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                texts.append(str(block["text"]))
        return "\n".join(texts) if texts else str(result)

    @staticmethod
    def _load_tools(tool_code: str) -> list[Any]:
        """tool モジュールから strands @tool 関数を収集する。"""
        if not tool_code:
            return []
        namespace: dict[str, Any] = {}
        try:
            exec(compile(tool_code, "<tools>", "exec"), namespace)  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool module import failed: %s", exc)
            return []
        # strands @tool デコード済み関数は _tool_spec 属性を持つ
        return [v for v in namespace.values() if callable(v) and hasattr(v, "_tool_spec")]


# ---------------------------------------------------------------------------
# ファクトリ
# ---------------------------------------------------------------------------

_MOCK_BY_ROLE: dict[str, type] = {
    "task": MockTaskModel,
    "judge": MockJudgeModel,
    "proposer": MockProposerModel,
    "selector": MockSelectorModel,
}

_BEDROCK_BY_ROLE: dict[str, type] = {
    "task": StrandsTaskModel,
    "judge": StrandsJudgeModel,
    "proposer": StrandsProposerModel,
    "selector": StrandsSelectorModel,
}


def build_model(role: str, provider: str, model_id: str = "", region: str = "us-west-2") -> Any:
    """役割とプロバイダからモデルクライアントを組み立てる。

    Args:
        role: task / judge / proposer / selector のいずれか。
        provider: mock / bedrock のいずれか。
        model_id: bedrock モデルID(.env の BEDROCK_MODEL_ID)。
        region: bedrock リージョン(.env の BEDROCK_REGION)。

    Returns:
        役割に対応するモデルクライアント。

    Raises:
        ValueError: 役割またはプロバイダが不正な場合。
    """
    if role not in ROLES:
        raise ValueError(f"未知の役割: {role}(有効: {ROLES})")
    if provider not in PROVIDERS:
        raise ValueError(f"未知のプロバイダ: {provider}(有効: {PROVIDERS})")
    if provider == "mock":
        return _MOCK_BY_ROLE[role]()
    return _BEDROCK_BY_ROLE[role](model_id=model_id, region=region)
