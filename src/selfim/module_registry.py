"""ModuleRegistry — ハーネスのモジュール分解と候補の隔離適用(論文B §II)。

責務:
- シードハーネス(src/selfim/harness/)を run ワークスペースへ複製(snapshot)
- 1候補=1モジュールの隔離適用(candidates/<candidate_id>/ 配下)
- 現行ハーネスとの unified diff 生成(表示・行数予算判定用)

設計判断 D-1: 候補適用は対象モジュールの全ファイル置換。diffは派生計算する。
安全上の制約: シードディレクトリは snapshot 以降一切書き換えない。

[DECISION 2026-11-22] Docker足場モジュール(entrypoint/dockerfile/requirements)を
管理対象に追加し、ワークスペースが「そのまま docker build 可能なビルド
コンテキスト」になるよう promote を拡張した。候補ハーネスの実行を
AgentCore互換コンテナで行う設計変更に伴う(論文B SG-1 の1候補=1モジュールは維持)。
"""

from __future__ import annotations

import difflib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from selfim.schemas import Proposal

logger = logging.getLogger(__name__)

# モジュール名 → ワークスペース内の相対パス。
# system_prompt: Context & State / skill: Agent Control / tool: Action Interface / verify: Verification & Recovery
# entrypoint: Agent Control(実行時コード) / dockerfile・requirements: デプロイ境界
MODULE_PATHS: dict[str, str] = {
    "system_prompt": "modules/system_prompt.txt",
    "skill": "modules/skill.md",
    "tool": "modules/tools.py",
    "verify": "modules/verify.py",
    "entrypoint": "scaffold/entrypoint.py",
    "dockerfile": "scaffold/Dockerfile",
    "requirements": "scaffold/requirements.txt",
}


@dataclass(frozen=True)
class ModuleCatalogEntry:
    """Proposerプロンプトに載せるモジュール目録の1エントリ。

    Attributes:
        path: ワークスペース内の相対パス(MODULE_PATHS と一致)。
        paper_b_component: 論文Bの4コンポーネント(またはデプロイ境界)との対応説明。
        guidance: 編集ガイド(小幅・一般的であるべき等の指示)。
    """

    path: str
    paper_b_component: str
    guidance: str


# モジュール目録(Proposerプロンプトと共有)。キーは MODULE_PATHS と完全一致させる。
# [DECISION 2026-11-22] 足場編集の危険性を下げるため、ガイドに契約(実行時コードは
# runtime/ が保証、data/ 評価データへのアクセス不可)を明記する。
MODULE_CATALOG: dict[str, ModuleCatalogEntry] = {
    "system_prompt": ModuleCatalogEntry(
        path="modules/system_prompt.txt",
        paper_b_component="Context & State",
        guidance="エージェントの役割・出力形式の指示。文言追加は小幅かつ一般的に。",
    ),
    "skill": ModuleCatalogEntry(
        path="modules/skill.md",
        paper_b_component="Agent Control(手順知識)",
        guidance="タスク手順の知識。特定タスクIDに依存する手順は追加しない。",
    ),
    "tool": ModuleCatalogEntry(
        path="modules/tools.py",
        paper_b_component="Action Interface",
        guidance="ツール定義(strands の @tool 形式)。既存関数の互換性を保つ。",
    ),
    "verify": ModuleCatalogEntry(
        path="modules/verify.py",
        paper_b_component="Verification & Recovery",
        guidance="完了前チェックリスト生成(build_checklist 契約を維持)。",
    ),
    "entrypoint": ModuleCatalogEntry(
        path="scaffold/entrypoint.py",
        paper_b_component="Agent Control(実行時コード)",
        guidance=(
            "run_task(task: dict) -> dict 契約を維持した小幅で一般的な編集のみ。"
            "HTTPサーバとプロバイダ選択は固定ランタイム(runtime/)が担うため触れない。"
            "評価データ・ルーブリックにはアクセスできない。"
        ),
    ),
    "dockerfile": ModuleCatalogEntry(
        path="scaffold/Dockerfile",
        paper_b_component="デプロイ境界",
        guidance=(
            "ベースイメージ・依存の変更のみ。data/ papers/ の COPY/ADD と"
            "ホストパスのマウントは禁止(論文B Table III)。"
        ),
    ),
    "requirements": ModuleCatalogEntry(
        path="scaffold/requirements.txt",
        paper_b_component="依存境界",
        guidance="パッケージ追加・バージョン変更のみ。不要な依存は増やさない。",
    ),
}


# モジュール名 → Proposer/モデルへ渡す sources 辞書のキー名。
# 前4つは既存(MockTaskModel/StrandsTaskModel)が期待するレガシーキーを維持する。
# [DECISION 2026-11-22] 足場モジュールのソースも Proposer に渡す。
_SOURCE_KEYS: dict[str, str] = {
    "system_prompt": "system_prompt",
    "skill": "skill",
    "tool": "tool_code",
    "verify": "verify_code",
    "entrypoint": "entrypoint_code",
    "dockerfile": "dockerfile",
    "requirements": "requirements",
}


def read_all_modules(base_dir: Path) -> dict[str, str]:
    """ハーネスディレクトリから7モジュール全てを読み込む。

    Args:
        base_dir: ハーネスディレクトリ(ワークスペースまたは候補ディレクトリ)。

    Returns:
        sources 辞書(system_prompt / skill / tool_code / verify_code /
        entrypoint_code / dockerfile / requirements)。欠損ファイルは空文字。
    """
    sources: dict[str, str] = {}
    for name, rel in MODULE_PATHS.items():
        p = base_dir / rel
        sources[_SOURCE_KEYS[name]] = p.read_text(encoding="utf-8") if p.exists() else ""
    return sources


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
        # [DECISION 2026-11-22] __pycache__/*.pyc も除外する。テスト実行でシードに
        # 生成されたキャッシュが候補ディレクトリ(=ビルドコンテキスト)へ混入するのを防ぐ。
        shutil.copytree(
            self.workspace_dir,
            candidate_dir,
            ignore=shutil.ignore_patterns("candidates", "__pycache__", "*.py[oc]"),
        )
        target = candidate_dir / MODULE_PATHS[proposal.target_module]
        target.write_text(proposal.new_content, encoding="utf-8")
        logger.debug("candidate %s applied (%s)", candidate_id, proposal.target_module)
        return candidate_dir

    def promote(self, candidate_dir: Path) -> None:
        """採択候補を現行ワークスペースへ昇格する(採択確定時のみ呼ぶ)。

        [DECISION 2026-11-22] modules/ に加え scaffold/ と runtime/ も昇格する。
        ワークスペースは常に「そのまま docker build できる完全なビルドコンテキスト」
        である必要があるため(runtime/ は不変だが自己完結性のため一緒に複製する)。
        """
        for subdir in ("modules", "scaffold", "runtime"):
            dest = self.workspace_dir / subdir
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(candidate_dir / subdir, dest)
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
