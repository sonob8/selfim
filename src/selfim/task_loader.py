"""task_loader — タスクスイート(tasks.yaml / rubric.yaml / splits.yaml)の読み込み。

data/tasks/<suite>/ 配下のファイルを読み込み、TaskSpec と Rubric を返す。
ユーザー提供のタスク・ルーブリックに差し替え可能であること(README how-to)が
このモジュールの存在理由。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import yaml

from selfim.schemas import Rubric, TaskSpec

logger = logging.getLogger(__name__)


def load_suite(suite_dir: Path) -> tuple[list[TaskSpec], Rubric]:
    """スイートディレクトリからタスクとルーブリックを読み込む。

    Args:
        suite_dir: data/tasks/<suite>/ のパス(tasks.yaml と rubric.yaml を含む)。

    Returns:
        (タスクリスト, ルーブリック)。

    Raises:
        FileNotFoundError: tasks.yaml / rubric.yaml が無い場合。
        ValueError: YAMLの構造が不正な場合。
    """
    tasks_path = suite_dir / "tasks.yaml"
    rubric_path = suite_dir / "rubric.yaml"
    if not tasks_path.exists():
        raise FileNotFoundError(f"tasks.yaml が見つかりません: {tasks_path}")
    if not rubric_path.exists():
        raise FileNotFoundError(f"rubric.yaml が見つかりません: {rubric_path}")

    tasks_raw = yaml.safe_load(tasks_path.read_text(encoding="utf-8")) or []
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ValueError("tasks.yaml はタスクのリストである必要があります")
    tasks = [TaskSpec.model_validate(t) for t in tasks_raw]

    ids = [t.task_id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("task_id が重複しています")

    rubric = Rubric.model_validate(yaml.safe_load(rubric_path.read_text(encoding="utf-8")))
    logger.info("suite loaded: %d tasks from %s", len(tasks), suite_dir)
    return tasks, rubric


def load_splits(suite_dir: Path, tasks: list[TaskSpec], run_seed: str) -> tuple[list[TaskSpec], list[TaskSpec]]:
    """タスクを evolve / held-out に分割する。

    splits.yaml がスイートディレクトリにあればそれを優先し、
    なければ run_seed に基づく決定的な半分ずつの分割を行う
    (論文A Appendix B.2: 一様ランダムだが再現性のためシード固定)。

    Args:
        suite_dir: スイートディレクトリ。
        tasks: 全タスク。
        run_seed: 実行ID(決定的分割のシード)。

    Returns:
        (evolve タスク, held-out タスク)。
    """
    splits_path = suite_dir / "splits.yaml"
    if splits_path.exists():
        raw = yaml.safe_load(splits_path.read_text(encoding="utf-8")) or {}
        by_id = {t.task_id: t for t in tasks}
        evolve = [by_id[i] for i in raw.get("evolve", [])]
        held = [by_id[i] for i in raw.get("held_out", [])]
        if evolve and held:
            return evolve, held
        logger.warning("splits.yaml が不完全なため決定的分割にフォールバックします")

    # 決定的分割: run_seed でタスク順を決め、evolve を偶数側にする
    # (論文A Appendix B.2: 奇数の場合は evolve 側が偶数を取る)
    # [FIX 2026-11-22] 組み込み hash() は PYTHONHASHSEED の影響でプロセス間で
    # 非決定だった。sha256 ベースに置換し、同一 run_seed なら必ず同一分割になるようにした
    # (新規コンテキストレビュー 2026-11-22 で指摘。held-out before/after 比較の前提)。
    ordered = sorted(
        tasks,
        key=lambda t: hashlib.sha256(f"{run_seed}:{t.task_id}".encode()).hexdigest(),
    )
    n_total = len(ordered)
    n_evolve = n_total // 2 if n_total % 2 == 0 else n_total - 1
    evolve = ordered[:n_evolve]
    held = ordered[n_evolve:]
    logger.info("splits: evolve=%d held_out=%d", len(evolve), len(held))
    return evolve, held
