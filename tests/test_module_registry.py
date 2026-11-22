"""ModuleRegistry の単体テスト(TP-2)。

検証観点:
- snapshot がシードをrunワークスペースへ複製し、シードは不変
- apply_candidate が対象モジュール1つだけ変更した候補ディレクトリを返す
- diff が unified diff を返す
- 7モジュール(足場entrypoint/dockerfile/requirementsを含む)の適用・昇格
- promote が modules/ + scaffold/ + runtime/ をすべて昇格すること
"""

from pathlib import Path

from selfim.module_registry import MODULE_CATALOG, MODULE_PATHS, ModuleRegistry
from selfim.schemas import Proposal

import pytest


@pytest.fixture()
def seed_dir(tmp_path: Path) -> Path:
    """テスト用シードハーネス(4コンテンツモジュール+足場scaffold+固定runtime)を作って返す。"""
    modules = tmp_path / "seed"
    (modules / "modules").mkdir(parents=True)
    (modules / "modules" / "system_prompt.txt").write_text("SEED PROMPT\n", encoding="utf-8")
    (modules / "modules" / "skill.md").write_text("SEED SKILL\n", encoding="utf-8")
    (modules / "modules" / "tools.py").write_text("SEED TOOLS\n", encoding="utf-8")
    (modules / "modules" / "verify.py").write_text("SEED VERIFY\n", encoding="utf-8")
    # 足場(編集可能モジュール)
    (modules / "scaffold").mkdir()
    (modules / "scaffold" / "entrypoint.py").write_text("SEED ENTRYPOINT\n", encoding="utf-8")
    (modules / "scaffold" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (modules / "scaffold" / "requirements.txt").write_text("strands-agents>=1.65.0\n", encoding="utf-8")
    # 固定ランタイム(Proposer編集外)
    (modules / "runtime").mkdir()
    (modules / "runtime" / "app.py").write_text("# FIXED RUNTIME\n", encoding="utf-8")
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


class TestScaffoldModules:
    """7モジュール化(足場entrypoint/dockerfile/requirements)の適用と昇格。"""

    def test_module_paths_cover_seven_modules(self) -> None:
        # [DECISION 2026-11-22] 足場も編集対象。MODULE_PATHS は7キーを持つ。
        assert set(MODULE_PATHS.keys()) == {
            "system_prompt",
            "skill",
            "tool",
            "verify",
            "entrypoint",
            "dockerfile",
            "requirements",
        }

    def test_module_catalog_matches_paths(self) -> None:
        # MODULE_CATALOG(Proposerプロンプト用)は MODULE_PATHS とキーが一致し、
        # 各エントリが(パス, 論文Bコンポーネント, 編集ガイド)を持つ。
        assert set(MODULE_CATALOG.keys()) == set(MODULE_PATHS.keys())
        for name, entry in MODULE_CATALOG.items():
            assert entry.path == MODULE_PATHS[name], f"{name} のパス不一致"
            assert entry.paper_b_component, f"{name} に論文Bコンポーネント記述がない"
            assert entry.guidance, f"{name} に編集ガイドがない"

    def test_apply_candidate_to_entrypoint(self, registry: ModuleRegistry) -> None:
        registry.snapshot()
        candidate_dir = registry.apply_candidate(
            _proposal("entrypoint", "def run_task(task):\n    return {}\n"), candidate_id="s1"
        )
        assert "def run_task" in (candidate_dir / "scaffold" / "entrypoint.py").read_text(encoding="utf-8")
        # 他のファイルは現行のまま
        assert "SEED PROMPT" in (candidate_dir / "modules" / "system_prompt.txt").read_text(encoding="utf-8")
        assert (candidate_dir / "runtime" / "app.py").exists()

    def test_apply_candidate_to_dockerfile(self, registry: ModuleRegistry) -> None:
        registry.snapshot()
        candidate_dir = registry.apply_candidate(
            _proposal("dockerfile", "FROM python:3.13-slim\n"), candidate_id="s2"
        )
        assert "3.13" in (candidate_dir / "scaffold" / "Dockerfile").read_text(encoding="utf-8")

    def test_missing_scaffold_file_in_seed_rejected(self, tmp_path: Path) -> None:
        # シードに scaffold/Dockerfile がなければ初期化時点で検出されること
        incomplete = tmp_path / "incomplete"
        (incomplete / "modules").mkdir(parents=True)
        for name in ("system_prompt.txt", "skill.md", "tools.py", "verify.py"):
            (incomplete / "modules" / name).write_text("X\n", encoding="utf-8")
        (incomplete / "scaffold").mkdir()
        (incomplete / "scaffold" / "entrypoint.py").write_text("X\n", encoding="utf-8")
        # scaffold/Dockerfile, scaffold/requirements.txt を意図的に置かない
        with pytest.raises(FileNotFoundError):
            ModuleRegistry(seed_dir=incomplete, workspace_dir=tmp_path / "run")

    def test_promote_copies_modules_scaffold_and_runtime(
        self, registry: ModuleRegistry
    ) -> None:
        # [DECISION 2026-11-22] ワークスペース=完全なビルドコンテキスト。
        # promote は modules/ だけでなく scaffold/ と runtime/ も昇格する。
        registry.snapshot()
        candidate_dir = registry.apply_candidate(
            _proposal("requirements", "strands-agents>=1.65.0\nbedrock-agentcore>=1.27.0\n"),
            candidate_id="s1",
        )
        registry.promote(candidate_dir)

        assert "bedrock-agentcore" in (registry.workspace_dir / "scaffold" / "requirements.txt").read_text(encoding="utf-8")
        assert (registry.workspace_dir / "runtime" / "app.py").exists()
        assert (registry.workspace_dir / "modules" / "verify.py").exists()
