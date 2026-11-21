"""自己改善ループ全体で共有するPydanticスキーマ定義。

このモジュールは src/selfim/ 配下の全コンポーネントの入出力型を一元的に定義する。
SIM(Self-Improvement Memory)のスキーマは論文A(Appendix D.3)に完全準拠させる。
制約:
- SIMのフィールド名・構造は変更しない(論文A D.3 がスキーマの正当解)。
- Proposal は必ず「既存根原因の引用 or 新規根原因の命名」のどちらか一方を要求する。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 改善対象モジュールの種別。論文Bの4コンポーネントに対応する。
# - system_prompt: Context & State
# - skill:        Agent Control(手順知識)
# - tool:         Action Interface
# - verify:       Verification & Recovery
ModuleName = Literal["system_prompt", "skill", "tool", "verify"]

# 失敗の帰属先。harness=ハーネス欠陥(改善対象)、model=モデルの能力限界(改善対象外)。
Attribution = Literal["harness", "model", "unknown"]


# ---------------------------------------------------------------------------
# タスクとルーブリック
# ---------------------------------------------------------------------------


class TaskSpec(BaseModel):
    """タスクスイート内の1タスク(data/tasks/<suite>/tasks.yaml の1エントリ)。

    Attributes:
        task_id: スイート内で一意なID。SIMの predicted/fixed/missed はこのIDで記録される。
        prompt: タスクエージェントに渡す指示文。
        context: タスクに渡す資料(任意)。評価データはハーネスから書き込めない場所で管理する。
        reasoning_steps_required: エージェントに明示させる思考ステップ(任意)。
    """

    task_id: str = Field(min_length=1)
    prompt: str
    context: str = ""
    reasoning_steps_required: list[str] = Field(default_factory=list)


class ScoringScale(BaseModel):
    """ルーブリックの採点範囲。"""

    min: int = 0
    max: int = 100


class RubricCriterion(BaseModel):
    """ルーブリックの採点基準1項目。weight の合計はスイート側で 1.0 にする。"""

    name: str
    weight: float = Field(gt=0.0, le=1.0)
    description: str = ""


class Rubric(BaseModel):
    """ジャッジに渡すルーブリック(data/tasks/<suite>/rubric.yaml)。"""

    scoring_scale: ScoringScale = Field(default_factory=ScoringScale)
    criteria: list[RubricCriterion]
    judge_instructions: str = ""


# ---------------------------------------------------------------------------
# 評価結果と失敗クラスタ
# ---------------------------------------------------------------------------


class EvaluationResult(BaseModel):
    """1タスク1rolloutのジャッジ結果。

    Attributes:
        score: ルーブリック重み付き総合スコア(0-100)。
        reason: 採点理由。
        missing_points: 「何が不足していたか」の具体的リスト。Proposerへの主要入力。
        attribution: 失敗の帰属(harness=改善対象 / model=対象外)。
    """

    task_id: str
    rollout: int = 0
    score: float = Field(ge=0.0, le=100.0)
    reason: str = ""
    missing_points: list[str] = Field(default_factory=list)
    attribution: Attribution = "unknown"
    output_text: str = ""  # タスクエージェントの生出力(トレース保持用)


class FailureCluster(BaseModel):
    """繰り返し失敗クラスタ(論文B §III)。

    count == 1 の失敗はクラスタ化しない(単発reflectionによる
    Phantom Guardrails を防ぐため、Proposerには渡さない)。
    """

    signature: str  # エラー署名(クラスタリングのキー)
    task_ids: list[str]
    count: int
    root_cause_hypothesis: str  # 根原因仮説ドラフト(ProposerがSIM照合に使う)
    attribution: Attribution = "harness"
    sample_missing_points: list[str] = Field(default_factory=list)


class SuiteFeedback(BaseModel):
    """evolve/held-out セット全体の評価フィードバック(論文Aの Ft に相当)。

    Attributes:
        score: タスク平均の集合スコア(J(H; M, X) のエボリューション側の値)。
        per_task: タスク×rolloutの全評価結果。
        clusters: 繰り返し失敗クラスタ(count==1 は含まない)。
    """

    label: str = "evolve"  # "evolve" or "held_out"
    round_no: int = 0
    score: float = 0.0
    per_task: list[EvaluationResult] = Field(default_factory=list)
    clusters: list[FailureCluster] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 提案と選択
# ---------------------------------------------------------------------------


class Proposal(BaseModel):
    """Proposerが生成する改善候補1件。

    制約:
    - cites_problem と new_problem_name は排他(どちらか一方のみ)。
    - target_module は1候補につき1モジュール(論文B Safe Guard 1)。
    - predicted は評価前に記録される予測タスクID(空は不可)。
    - new_content は対象モジュールの全内容(設計判断 D-1)。
    """

    cites_problem: Optional[str] = None
    new_problem_name: Optional[str] = None
    target_module: ModuleName
    what: str  # 編集内容とその理由
    new_content: str  # 対象モジュールの新しい全内容
    predicted: list[str] = Field(min_length=1)
    rationale: str = ""  # SIMの過去試行と突き合わせた判断

    @model_validator(mode="after")
    def _validate_citation_exclusive(self) -> "Proposal":
        """根原因の引用と新規命名は排他であることを検証する。"""
        if (self.cites_problem is None) == (self.new_problem_name is None):
            raise ValueError(
                "cites_problem と new_problem_name はどちらか一方のみ指定すること"
            )
        return self

    @property
    def problem_key(self) -> str:
        """SIM記録時の根原因キーを返す(既存ID or 新規名)。"""
        return self.cites_problem if self.cites_problem else self.new_problem_name  # type: ignore[return-value]


class GateResult(BaseModel):
    """ValidationGate の判定結果。"""

    passed: bool
    reasons: list[str] = Field(default_factory=list)  # 拒否理由(合格時は空)


class CandidateEvaluation(BaseModel):
    """実行前スクリーニングを通過し、検証ゲートと再評価を経た候補1件の記録。"""

    candidate_id: str
    proposal: Proposal
    gate: GateResult
    score: float = 0.0  # 検証ゲート通過後の再評価スコア(拒否時は 0)
    diff_lines: int = 0


class SelectionResult(BaseModel):
    """評価後選択の結果。accepted_candidate_id が None の場合は全不採択。"""

    accepted_candidate_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# SIM (論文A Appendix D.3 準拠)
# ---------------------------------------------------------------------------


class SimAttempt(BaseModel):
    """根原因に対する試行1件。フィールド名は論文A D.3 に完全一致させる。

    Attributes:
        round: 試行が行われたラウンド番号。
        sample: 候補のサンプリング識別子(s1, s2, ...)。
        accepted: セレクタがそのラウンドで採択したか(予測の一部が外れても true になり得る)。
        backfilled: 成果が後から埋められたか(論文Aのフィールド)。
        what: 編集内容の説明(truncationしない)。
        files: 編集対象ファイル。
        predicted: 評価前に記録された修正予測タスクID。
        fixed / missed: 評価後に確定した修正成功/失敗タスクID。
        passed / total: その試行の evolve 評価における成功/総rollout数。
        domain: 記録時の設定(タスクスイート名)。
    """

    round: int
    sample: str = "s1"
    accepted: bool = True
    backfilled: bool = False
    what: str
    files: str
    predicted: list[str] = Field(default_factory=list)
    fixed: list[str] = Field(default_factory=list)
    missed: list[str] = Field(default_factory=list)
    passed: int = 0
    total: int = 0
    domain: str = ""


class SimProblem(BaseModel):
    """根原因エントリ。試行を時系列で保持する。"""

    id: str  # P1, P2, ...
    name: str  # 根原因仮説(テキスト完全一致でマージ判定する)
    opened_round: int
    attempts: list[SimAttempt] = Field(default_factory=list)


class SimRecord(BaseModel):
    """SIMデータベース1ファイル分(output/runs/<run_id>/sim.json)。

    Attributes:
        next_id: 次に採番する問題ID(P{n})。
        carried_from: 継続元ランの情報(CRSI継続時)。新規runは None。
        aliases: マージで吸収された問題ID → 吸収先ID(例: {"P6": "P5"})。
        problems: 根原因エントリのリスト。
    """

    next_id: int = 1
    carried_from: Optional[dict[str, str]] = None
    aliases: dict[str, str] = Field(default_factory=dict)
    problems: list[SimProblem] = Field(default_factory=list)

    def find_problem(self, key: str) -> Optional[SimProblem]:
        """IDまたは根原因名でエントリを検索する。aliases も解決する。"""
        resolved = self.aliases.get(key, key)
        for p in self.problems:
            if p.id == resolved or p.name == resolved:
                return p
        return None


class RoundRecord(BaseModel):
    """1ラウンドの実行記録(RunReport の要素、履歴 Dt の永続化形式)。"""

    round_no: int
    base_score: float
    screened_out: list[str] = Field(default_factory=list)  # 拒否理由のリスト
    evaluated: list[CandidateEvaluation] = Field(default_factory=list)
    accepted_candidate_id: Optional[str] = None
    accepted_score: Optional[float] = None  # 不採択時は None
    sim_updated: bool = False


class RunReport(BaseModel):
    """ループ実行全体のレポート(output/runs/<run_id>/report.json)。"""

    run_id: str
    suite: str
    provider: str
    n_rounds: int
    k_candidates: int
    rounds: list[RoundRecord] = Field(default_factory=list)
    held_out_before: float = 0.0
    held_out_after: float = 0.0
    evolve_before: float = 0.0
    evolve_final: float = 0.0
