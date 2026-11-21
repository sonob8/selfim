"""完了前チェックリスト生成(Verification & Recovery モジュール)。

タスク完了前に出力へ追加すべき検証項目を返す。改善ループの候補は
このファイルを置き換える形で編集される(mockモードでは build_checklist
関数が呼出記録の対象)。
"""

from __future__ import annotations


def build_checklist(task_id: str) -> list[str]:
    """タスクIDに対応する完了前チェック項目を返す。

    Args:
        task_id: タスクID(現シードでは全タスク共通の項目を返す)。

    Returns:
        チェック項目のリスト。
    """
    return ["結論の再確認"]
