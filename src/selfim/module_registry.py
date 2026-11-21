"""ModuleRegistry — ハーネスのモジュール分解と候補の隔離適用(論文B §II)。

責務:
- シードハーネス(src/selfim/harness/)を run ワークスペースへ複製(snapshot)
- 1候補=1モジュールの隔離適用(candidates/<candidate_id>/ 配下)
- 現行ハーネスとの unified diff 生成(表示・行数予算判定用)

設計判断 D-1: 候補適用は対象モジュールの全ファイル置換。diffは派生計算する。
安全上の制約: シードディレクトリは snapshot 以降一切書き換えない。
"""

from __future__ import annotations

import difflib
import logging
import shutil
from pathlib import Path

from selfim.schemas import Proposal

logger = logging.getLogger(__name__)

# モジュール名 → ワークスペース内の相対パス。
# system_prompt: Context & State / skill: Agent Control / tool: Action Interface / verify: Verification & Recovery
MODULE_PATHS: dict[str, str] = {
    "system_prompt": "modules/system_prompt.txt",
    "skill": "modules/skill.md",
    "tool": "modules/tools.py",
    "verify": "modules/verify.py",
}


class ModuleRegistry:
    """シードとrunワークスペースを束縛し、モジュール操作を提供する。

    Attributes:
        seed_dir: シードハーネスのディレクトリ(書き換えない)。
        workspace_dir: runワークスペース(現行ハーネスの作業場所)。
    """

    def __init__(self, seed_dir: Path, workspace_dir: Path) -> None:
        """シードとワークスペースを束縛する。

        Args:
            seed_dir: シードハーネスディレクトリ。
            workspace_dir: runワークスペースディレクトリ(存在しなければ snapshot 時に作成)。

        Raises:
            FileNotFoundError: シードに必要なモジュールファイルが揃っていない場合。
        """
        self.seed_dir = seed_dir
        self.workspace_dir = workspace_dir
        for name, rel in MODULE_PATHS.items():
            if not (seed_dir / rel).exists():
                raise FileNotFoundError(f"シードにモジュール {name} ({rel}) がありません")

    def snapshot(self) -> None:
        """シードをrunワークスペースへ複製する。既存ワークスペースは上書きする。"""
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir)
        shutil.copytree(self.seed_dir, self.workspace_dir)
        logger.info("workspace snapshot created at %s", self.workspace_dir)

    def module_names(self) -> list[str]:
        """管理対象モジュール名のリストを返す。"""
        return list(MODULE_PATHS.keys())

    def read_module(self, name: str, base_dir: Path | None = None) -> str:
        """モジュールの現在内容を返す。

        Args:
            name: モジュール名(system_prompt/skill/tool/verify)。
            base_dir: 読み込み基点(既定はワークスペース)。

        Returns:
            モジュールファイルの全文。
        """
        root = base_dir if base_dir is not None else self.workspace_dir
        return (root / MODULE_PATHS[name]).read_text(encoding="utf-8")

    def apply_candidate(self, proposal: Proposal, candidate_id: str) -> Path:
        """候補を candidates/<candidate_id>/ に隔離適用し、候補ハーネスのパスを返す。

        現行ワークスペースには一切書き込まない(採択確定後に昇格する)。
        適用は対象モジュール1つの全ファイル置換(設計判断 D-1)。

        Args:
            proposal: 適用する候補(単一 target_module を含む)。
            candidate_id: 候補の識別子(s1, s2, ...)。

        Returns:
            候補ハーネスのディレクトリパス。
        """
        candidate_dir = self.workspace_dir / "candidates" / candidate_id
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)
        shutil.copytree(self.workspace_dir, candidate_dir, ignore=shutil.ignore_patterns("candidates"))
        target = candidate_dir / MODULE_PATHS[proposal.target_module]
        target.write_text(proposal.new_content, encoding="utf-8")
        logger.debug("candidate %s applied (%s)", candidate_id, proposal.target_module)
        return candidate_dir

    def promote(self, candidate_dir: Path) -> None:
        """採択候補を現行ワークスペースへ昇格する(採択確定時のみ呼ぶ)。"""
        shutil.rmtree(self.workspace_dir / "modules")
        shutil.copytree(candidate_dir / "modules", self.workspace_dir / "modules")
        logger.info("candidate %s promoted to current workspace", candidate_dir.name)

    def diff(self, name: str, candidate_dir: Path) -> str:
        """現行ワークスペースと候補の unified diff を返す。

        Args:
            name: モジュール名。
            candidate_dir: 候補ハーネスディレクトリ。

        Returns:
            unified diff 文字列(変更がなければ空文字)。
        """
        current = self.read_module(name).splitlines(keepends=True)
        candidate = self.read_module(name, base_dir=candidate_dir).splitlines(keepends=True)
        return "".join(difflib.unified_diff(current, candidate, fromfile=f"current/{name}", tofile=f"candidate/{name}"))

    def diff_line_count(self, name: str, candidate_dir: Path) -> int:
        """diff の追加・削除行数(コンテキスト行を除く)を返す。

        Args:
            name: モジュール名。
            candidate_dir: 候補ハーネスディレクトリ。

        Returns:
            '+' または '-' で始まるdiff行の数。
        """
        return sum(
            1
            for line in self.diff(name, candidate_dir).splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
