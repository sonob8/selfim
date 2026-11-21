"""ModuleRegistry の単体テスト(TP-2)。

検証観点:
- snapshot がシードをrunワークスペースへ複製し、シードは不変
- apply_candidate が対象モジュール1つだけ変更した候補ディレクトリを返す
- diff が unified diff を返す
"""

from pathlib import Path

from selfim.module_registry import ModuleRegistry
from selfim.schemas import Proposal

import pytest


@pytest.fixture()
def seed_dir(tmp_path: Path) -> Path:
    """テスト用シードハーネス(4モジュール)を作って返す。"""
    modules = tmp_path / "seed"
    (modules / "modules").mkdir(parents=True)
    (modules / "modules" / "system_prompt.txt").write_text("SEED PROMPT\n", encoding="utf-8")
    (modules / "modules" / "skill.md").write_text("SEED SKILL\n", encoding="utf-8")
    (modules / "modules" / "tools.py").write_text("SEED TOOLS\n", encoding="utf-8")
    (modules / "modules" / "verify.py").write_text("SEED VERIFY\n", encoding="utf-8")
    return modules


@pytest.fixture()
def registry(tmp_path: Path, seed_dir: Path) -> ModuleRegistry:
    """runワークスペースを束縛したModuleRegistryを返す。"""
    return ModuleRegistry(seed_dir=seed_dir, workspace_dir=tmp_path / "run")


def _proposal(module: str, content: str) -> Proposal:
    """テスト用Proposalを生成するヘルパー。"""
    return Proposal(
        new_problem_name="X",
        target_module=module,  # type: ignore[arg-type]
        what="edit",
        new_content=content,
        predicted=["t1"],
    )


class TestSnapshot:
    def test_snapshot_copies_seed_and_seed_stays_immutable(
        self, registry: ModuleRegistry, seed_dir: Path
    ) -> None:
        registry.snapshot()
        ws_prompt = registry.read_module("system_prompt")
        assert "SEED PROMPT" in ws_prompt

        # ワークスペース側のファイルを直接書き換えてもシードは変わらない
        ws_file = registry.workspace_dir / "modules" / "system_prompt.txt"
        ws_file.write_text("CHANGED\n", encoding="utf-8")
        assert "SEED PROMPT" in (seed_dir / "modules" / "system_prompt.txt").read_text(encoding="utf-8")


class TestApplyCandidate:
    def test_apply_creates_candidate_dir_with_only_target_changed(
        self, registry: ModuleRegistry
    ) -> None:
        registry.snapshot()
        candidate_dir = registry.apply_candidate(
            _proposal("system_prompt", "NEW PROMPT\n"), candidate_id="s1"
        )

        # 対象モジュールだけ変わり、他モジュールは現行のまま
        assert "NEW PROMPT" in (candidate_dir / "modules" / "system_prompt.txt").read_text(encoding="utf-8")
        assert "SEED SKILL" in (candidate_dir / "modules" / "skill.md").read_text(encoding="utf-8")
        # 現行ワークスペースには反映されない(採択時に別途昇格する)
        assert "NEW PROMPT" not in registry.read_module("system_prompt")

    def test_diff_returns_unified_diff(self, registry: ModuleRegistry) -> None:
        registry.snapshot()
        candidate_dir = registry.apply_candidate(
            _proposal("system_prompt", "NEW PROMPT\n"), candidate_id="s1"
        )
        d = registry.diff("system_prompt", candidate_dir)
        assert "---" in d and "+++" in d
        assert "-SEED PROMPT" in d and "+NEW PROMPT" in d

    def test_diff_line_count_matches_difflib(self, registry: ModuleRegistry) -> None:
        registry.snapshot()
        candidate_dir = registry.apply_candidate(
            _proposal("system_prompt", "NEW PROMPT\n"), candidate_id="s1"
        )
        n = registry.diff_line_count("system_prompt", candidate_dir)
        assert n == 2  # -SEED PROMPT と +NEW PROMPT の2行
