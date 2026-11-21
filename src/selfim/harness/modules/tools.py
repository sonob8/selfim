"""シードのツール定義(Action Interface モジュール)。

推論タスクで使える最小のツールセット。改善ループの候補はこのファイルを
置き換える形で編集される(mockモードでは outline 関数が呼出記録の対象)。
"""

from __future__ import annotations


def outline(steps: list[str]) -> list[str]:
    """思考ステップの一覧を整形して返す。

    Args:
        steps: 思考ステップの名前リスト。

    Returns:
        番号付きのステップ一覧。
    """
    return [f"{i}. {s}" for i, s in enumerate(steps, start=1)]
