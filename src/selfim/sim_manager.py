"""SIMManager — Self-Improvement Memory データベースの管理(論文A §3.2, D.1, D.3 完全準拠)。

責務:
- 1 run につき1つの JSON ファイル(sim.json)の読み書き
- 採択候補の2段階記録(評価前に予測 → 評価後に成果)
- 根原因テキスト完全一致エントリの決定的マージ + aliases 記録
- Proposer/Selector への全エントリ全文レンダリング(検索・truncationなし)

不変条件:
- 不採択ラウンドでは record_predicted/record_outcome を呼ばない = SIM不変 (U(M,∅)=M)
- マージで試行履歴は失われない
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from selfim.schemas import Proposal, SimAttempt, SimProblem, SimRecord

logger = logging.getLogger(__name__)


class SIMManager:
    """sim.json を束縛し、SIMの更新操作を提供する。

    Attributes:
        path: SIMファイルのパス(output/runs/<run_id>/sim.json)。
        has_pending_outcome: record_predicted 済みで record_outcome 未実施の試行があるか。
    """

    def __init__(self, path: Path) -> None:
        """SIMファイルパスを束縛する。ファイルが存在すれば読み込む。

        Args:
            path: sim.json のパス。存在しない場合は初回 save 時に作成される。
        """
        self.path = path
        self._pending_key: Optional[str] = None
        self._pending_round: Optional[int] = None

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------

    def load(self) -> SimRecord:
        """SIMファイルを読み込む。ファイルがなければ空SIMを返す。

        Returns:
            読み込んだ(または新規の)SimRecord。
        """
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            record = SimRecord.model_validate(raw)
        else:
            record = SimRecord()
        return record

    def save(self, record: SimRecord) -> None:
        """SIMファイルに書き込む(親ディレクトリがなければ作成する)。

        Args:
            record: 書き込むSimRecord。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.debug("SIM saved: %s (%d problems)", self.path, len(record.problems))

    # ------------------------------------------------------------------
    # 2段階記録 (設計判断 D-2)
    # ------------------------------------------------------------------

    def record_predicted(
        self, record: SimRecord, proposal: Proposal, round_no: int, sample_id: str,
        domain: str = "",
    ) -> SimRecord:
        """採択候補の予測を評価の前に記録する(論文A §3.2)。

        proposal.problem_key(cites_problem or new_problem_name)で
        既存エントリに追記するか新規エントリを作るかを決める。
        マッチング判断はProposer側で行う(類似度モデルは使わない、論文A D.1)。

        Args:
            record: 現在のSIM。
            proposal: 採択された候補。
            round_no: 現在のラウンド番号。
            sample_id: 候補のサンプリング識別子(s1, s2, ...)。
            domain: 記録時の設定(タスクスイート名)。

        Returns:
            予測を追記した新しいSimRecord(引数recordは変更しない)。
        """
        key = proposal.problem_key
        attempt = SimAttempt(
            round=round_no,
            sample=sample_id,
            accepted=True,
            backfilled=False,
            what=proposal.what,
            files=proposal.target_module,
            predicted=list(proposal.predicted),
            domain=domain,
        )
        # イミュータブル更新: 元のrecord・既存エントリは一切変更しない
        existing = self.find_problem_by_key(record, key)
        if existing is not None:
            problem_id = existing.id
            updated_problems = [
                p.model_copy(update={"attempts": [*p.attempts, attempt]}) if p.id == problem_id else p
                for p in record.problems
            ]
            new_next_id = record.next_id
        else:
            # [DECISION 2026-11-21] 新規エントリは model_copy ベースで構築し、
            # 引数のrecordに直接appendしない(グローバル規約のイミュータブル原則)。
            problem_id = f"P{record.next_id}"
            new_problem = SimProblem(
                id=problem_id, name=key, opened_round=round_no, attempts=[attempt]
            )
            updated_problems = [*record.problems, new_problem]
            new_next_id = record.next_id + 1
        new_record = record.model_copy(update={"problems": updated_problems, "next_id": new_next_id})
        self._pending_key = problem_id
        self._pending_round = round_no
        logger.info("SIM: predicted recorded -> %s (round %d)", problem_id, round_no)
        return new_record

    def record_outcome(
        self, record: SimRecord, fixed: list[str], missed: list[str],
        passed: int, total: int,
    ) -> SimRecord:
        """評価後に直前の record_predicted 対象へ成果を書き込む(論文A §3.2)。

        Args:
            record: 予測記録済みのSIM。
            fixed: 予測どおり修正されたタスクID。
            missed: 予測に反して修正されなかったタスクID。
            passed / total: evolve評価の成功/総rollout数。
            record の pending attempt (backfilled=False) にのみ作用する。

        Returns:
            成果を埋めた新しいSimRecord。

        Raises:
            RuntimeError: record_predicted が先に呼ばれていない場合。
        """
        if self._pending_key is None:
            raise RuntimeError("record_outcome の前に record_predicted を呼ぶこと")
        key, round_no = self._pending_key, self._pending_round
        # [FIX 2026-11-21] 同一ラウンドで複数の未記録試行が存在する場合、どれに成果を
        # 書くべきか特定できないため RuntimeError とする。通常フローは1ラウンド1採択
        # なので必ず1件のみヒットする。この例外が出たら呼出側のフロー破綻を意味する。
        pending_matches = [
            (p.id, a)
            for p in record.problems if p.id == key
            for a in p.attempts if a.round == round_no and not a.backfilled
        ]
        if len(pending_matches) != 1:
            raise RuntimeError(
                f"成果を書き込む未記録試行が {len(pending_matches)} 件あります(1件であるべき)"
            )
        updated_problems = []
        for p in record.problems:
            if p.id != key:
                updated_problems.append(p)
                continue
            new_attempts = []
            for a in p.attempts:
                if a.round == round_no and not a.backfilled:
                    new_attempts.append(
                        a.model_copy(
                            update={
                                "fixed": list(fixed),
                                "missed": list(missed),
                                "passed": passed,
                                "total": total,
                                "backfilled": True,
                            }
                        )
                    )
                else:
                    new_attempts.append(a)
            updated_problems.append(p.model_copy(update={"attempts": new_attempts}))
        self._pending_key = None
        self._pending_round = None
        logger.info("SIM: outcome recorded -> %s (fixed=%d missed=%d)", key, len(fixed), len(missed))
        return record.model_copy(update={"problems": updated_problems})

    @property
    def has_pending_outcome(self) -> bool:
        """record_predicted 済みで成果未記録の試行があるか。"""
        return self._pending_key is not None

    # ------------------------------------------------------------------
    # マッチングとマージ (論文A D.1)
    # ------------------------------------------------------------------

    @staticmethod
    def find_problem_by_key(record: SimRecord, key: str) -> Optional[SimProblem]:
        """ID・根原因名・alias のいずれかでエントリを検索する。"""
        resolved = record.aliases.get(key, key)
        for p in record.problems:
            if p.id == resolved or p.name == resolved:
                return p
        return None

    def merge_by_exact_name(self, record: SimRecord) -> SimRecord:
        """根原因名(name)が完全一致するエントリを決定的マージする(論文A D.1)。

        吸収される側のIDは aliases に記録し、試行はすべて吸収先へ移す。

        Args:
            record: マージ対象のSIM。

        Returns:
            マージ済みの新しいSimRecord。
        """
        by_name: dict[str, int] = {}  # name -> merged_problems 内のインデックス
        merged_problems: list[SimProblem] = []
        new_aliases = dict(record.aliases)

        for problem in record.problems:
            if problem.name in by_name:
                # [FIX 2026-11-21] 累積は merged_problems 側の現状を起点にする。
                # 以前は元のrecordから吸収先を取得していたため、同名3件以上で
                # 先に吸収した試行が上書きされるデータ喪失バグがあった
                # (diff-review 2026-11-21 で検出)。試行履歴は決して失わないこと。
                idx = by_name[problem.name]
                keep = merged_problems[idx]
                new_aliases[problem.id] = keep.id
                merged_problems[idx] = keep.model_copy(
                    update={"attempts": [*keep.attempts, *problem.attempts]}
                )
            else:
                by_name[problem.name] = len(merged_problems)
                merged_problems.append(problem)

        return record.model_copy(update={"problems": merged_problems, "aliases": new_aliases})

    # ------------------------------------------------------------------
    # プロンプト用レンダリング (論文A §3.2: 全エントリ・truncationなし)
    # ------------------------------------------------------------------

    def render_for_prompt(self, record: SimRecord) -> str:
        """全エントリをProposer/Selector用テキストに整形する。

        検索もtruncationも行わない。エントリが数百に達するとコンテキスト上限に
        当たるが、これは論文Aが明示的に選んだ方式(全文参照)である。

        Args:
            record: レンダリング対象のSIM。

        Returns:
            全エントリを含むテキスト。
        """
        if not record.problems:
            return "(SIMは空です。過去の改善試行はありません。)"
        lines: list[str] = []
        for p in record.problems:
            lines.append(f"[{p.id}] {p.name} (opened_round={p.opened_round})")
            for a in p.attempts:
                outcome = "未記録(評価待ち)" if not a.backfilled else (
                    f"fixed={a.fixed} missed={a.missed} passed={a.passed}/{a.total}"
                )
                lines.append(
                    f"  - round {a.round} ({a.sample}, domain={a.domain}, files={a.files}): {a.what}\n"
                    f"    predicted={a.predicted} -> {outcome}"
                )
        if record.aliases:
            lines.append(f"(aliases: {record.aliases})")
        return "\n".join(lines)
