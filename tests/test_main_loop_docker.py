"""docker実行系のE2Eテスト(実Docker・mockプロバイダ・キー不要)。

検証観点(マーカ docker / docker_e2e。Dockerがない環境はスキップ):
- 全体ループ(評価→提案→スクリーニング→ビルド→コンテナ評価→ゲート→採択)が
  mockプロバイダで完走し、スコアが向上すること(local E2Eと同じ改善ダイナミクス)
- SIM が採択に伴い更新されること
- 出荷成果物(ship/agent-image.tar, ship.json)が生成されること
- 出荷イメージが /ping と /invocations(AgentCore互換IF)に応答すること

注意: 初回実行は python:3.12-slim の取得と strands-agents 等の pip install を
含むため数分かかる(2回目以降はレイヤキャッシュで高速化される)。
コンテナ内 bedrock 経路は実クレデンシャルがなく検証不能(対象外、docs/docker.md 参照)。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from selfim.config_loader import load_settings
from selfim.evaluator import Evaluator
from selfim.main_loop import MainLoop
from selfim.models import build_model
from selfim.module_registry import ModuleRegistry
from selfim.proposer import Proposer
from selfim.selector import Selector
from selfim.shipper import Shipper
from selfim.sim_manager import SIMManager
from selfim.task_loader import load_suite
from selfim.validation_gate import ValidationGate

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_HARNESS = REPO_ROOT / "src" / "selfim" / "harness"


@pytest.fixture()
def suite(tmp_path: Path):
    """test_main_loop.py と同一の最小スイート(mock改善ダイナミクスの比較可能性)。"""
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "tasks.yaml").write_text(
        '- task_id: legal-001\n  prompt: 判断せよ\n  context: "事実: 契約の事案"\n'
        "  reasoning_steps_required: [要件確認, 催告検討, 反対解釈, 整合チェック]\n"
        '- task_id: legal-002\n  prompt: 判断せよ\n  context: "事実: 不法行為の事案"\n'
        "  reasoning_steps_required: [要件確認, 構成要件, 反対解釈, 整合チェック]\n"
        '- task_id: legal-003\n  prompt: 判断せよ\n  context: "事実: 相続の事案"\n'
        "  reasoning_steps_required: [相続人確定, 代襲検討, 反対解釈, 整合チェック]\n",
        encoding="utf-8",
    )
    (suite_dir / "rubric.yaml").write_text(
        "criteria:\n"
        "  - name: 思考ステップの完全性\n    weight: 0.4\n    description: x\n"
        "  - name: 結論の妥当性\n    weight: 0.3\n    description: x\n"
        "  - name: 根拠の具体性\n    weight: 0.2\n    description: x\n"
        "  - name: 反対解釈の検討\n    weight: 0.1\n    description: x\n",
        encoding="utf-8",
    )
    tasks, rubric = load_suite(suite_dir)
    return tasks, rubric


@pytest.mark.docker_e2e
@pytest.mark.docker
class TestDockerEndToEnd:
    def test_full_docker_loop_improves_and_ships(
        self, suite, tmp_path: Path, docker_available: bool
    ) -> None:
        if not docker_available:
            pytest.skip("Docker デーモンが利用できないためスキップ")

        from selfim.docker_executor import DockerAgentRunner

        tasks, rubric = suite
        run_id = "dockere2e"
        run_dir = tmp_path / "runs" / run_id
        settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
        sim_manager = SIMManager(path=run_dir / "sim.json")

        def runner_factory(harness_dir: Path) -> DockerAgentRunner:
            """run_loop.py と同じ docker 実行器ファクトリ。"""
            label = "base" if harness_dir.name == "harness" else harness_dir.name
            return DockerAgentRunner(
                harness_dir=harness_dir,
                model_provider="mock",
                run_id=run_id,
                candidate_label=label,
                settings=settings.docker,
                logs_dir=run_dir / "logs",
            )

        loop = MainLoop(
            evaluator=Evaluator(model=build_model("judge", "mock")),
            proposer=Proposer(
                model=build_model("proposer", "mock"), sim_manager=sim_manager
            ),
            selector=Selector(
                model=build_model("selector", "mock"),
                diff_line_budget=settings.diff_line_budget,
                executor="docker",
            ),
            sim_manager=sim_manager,
            module_registry=ModuleRegistry(
                seed_dir=SEED_HARNESS, workspace_dir=run_dir / "harness"
            ),
            gate=ValidationGate(diff_line_budget=settings.diff_line_budget),
            task_model=None,  # docker実行系: タスクモデルはコンテナ内で動く
            rubric=rubric,
            run_dir=run_dir,
            rollouts_per_task=1,  # E2E時間短縮(改善判定には十分)
            suite_name="legal_judgment",
            runner_factory=runner_factory,
        )
        report = loop.run(
            tasks=tasks, splits=(tasks[:2], tasks[2:]), n_rounds=1, k_candidates=2
        )

        # 1) スコア向上(local E2Eと同じ改善ダイナティクス: round1 で採択)
        assert report.evolve_final > report.evolve_before
        assert any(r.accepted_candidate_id for r in report.rounds)

        # 2) SIM が更新されていること
        sim_data = json.loads((run_dir / "sim.json").read_text(encoding="utf-8"))
        assert len(sim_data["problems"]) >= 1

        # 3) 出荷成果物
        manifest = Shipper().ship(
            harness_dir=run_dir / "harness",
            report=report,
            sim_record=sim_manager.load(),
            run_id=run_id,
            executor="docker",
            model_provider="mock",
            args={"suite": "legal_judgment"},
        )
        assert manifest is not None
        assert Path(manifest.tar_path).exists()
        assert (run_dir / "ship" / "ship.json").exists()

        # 4) 出荷イメージがAgentCore互換IFに応答すること(スモーク)
        self._assert_image_serves_invocations(manifest.image_tag)

    @staticmethod
    def _assert_image_serves_invocations(image_tag: str) -> None:
        """出荷イメージを起動し /ping と /invocations の応答を確認する。"""
        container = f"{image_tag}-smoke"
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
        try:
            run = subprocess.run(
                ["docker", "run", "-d", "--name", container,
                 "-p", "127.0.0.1:18080:8080", image_tag],
                capture_output=True, text=True, timeout=60, check=True,
            )
            assert run.stdout.strip(), "コンテナ起出力がない"

            # /ping が通るまで待つ(最大30秒)
            import time
            import urllib.request

            deadline = time.monotonic() + 30
            ping_ok = False
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen("http://127.0.0.1:18080/ping", timeout=2) as resp:
                        ping_ok = resp.status == 200
                        if ping_ok:
                            break
                except OSError:
                    time.sleep(0.5)
            assert ping_ok, "出荷イメージが /ping に応答しない"

            req = urllib.request.Request(
                "http://127.0.0.1:18080/invocations",
                data=json.dumps({
                    "task_id": "smoke-001", "prompt": "判断せよ",
                    "context": "事実: スモーク", "reasoning_steps_required": ["確認"],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                envelope = json.loads(resp.read())
            assert envelope["ok"] is True
            assert "結論" in envelope["output_text"]
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
