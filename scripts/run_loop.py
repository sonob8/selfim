"""selfim 改善ループのCLIエントリ。

使い方:
    uv run python scripts/run_loop.py --suite legal_judgment --provider mock --rounds 2 --candidates 2

プロジェクトルートで実行すること(docs/tutorial.md 参照)。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# プロジェクトルート/src を import 解決に追加
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from selfim.config_loader import load_settings  # noqa: E402
from selfim.evaluator import Evaluator  # noqa: E402
from selfim.main_loop import MainLoop  # noqa: E402
from selfim.models import build_model  # noqa: E402
from selfim.module_registry import ModuleRegistry  # noqa: E402
from selfim.proposer import Proposer  # noqa: E402
from selfim.selector import Selector  # noqa: E402
from selfim.sim_manager import SIMManager  # noqa: E402
from selfim.schemas import RunReport  # noqa: E402
from selfim.task_loader import load_splits, load_suite  # noqa: E402
from selfim.validation_gate import ValidationGate  # noqa: E402

logger = logging.getLogger("run_loop")


def parse_args() -> argparse.Namespace:
    """CLI引数を解析する。

    Returns:
        解析済みの引数。
    """
    parser = argparse.ArgumentParser(description="selfim 改善ループ")
    parser.add_argument("--suite", required=True, help="data/tasks/ 配下のスイート名")
    parser.add_argument(
        "--provider",
        default=None,
        help="mock | bedrock(未指定は .env の SELFIM_MODEL_PROVIDER、さらに未設定なら mock)",
    )
    parser.add_argument("--rounds", type=int, default=None, help="改善ラウンド数")
    parser.add_argument("--candidates", type=int, default=None, help="1ラウンドの候補数")
    parser.add_argument("--run-id", default=None, help="run識別子(未指定はタイムスタンプ)")
    return parser.parse_args()


def main() -> int:
    """ループを実行し、レポートを output/runs/<run_id>/report.json に書く。

    Returns:
        終了コード(成功=0)。
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(PROJECT_ROOT / ".env")

    import os

    args = parse_args()
    settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    provider = args.provider or os.environ.get("SELFIM_MODEL_PROVIDER", "mock")
    n_rounds = args.rounds if args.rounds is not None else settings.n_rounds
    k_candidates = args.candidates if args.candidates is not None else settings.k_candidates
    run_id = args.run_id or f"{args.suite}-{provider}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = PROJECT_ROOT / "output" / "runs" / run_id

    # タスクと分割(論文A Appendix B.2: evolve/held-out は厳密に分離)
    suite_dir = PROJECT_ROOT / "data" / "tasks" / args.suite
    tasks, rubric = load_suite(suite_dir)
    evolve, held_out = load_splits(suite_dir, tasks, run_id)
    logger.info("suite=%s tasks=%d evolve=%d held_out=%d provider=%s",
                args.suite, len(tasks), len(evolve), len(held_out), provider)

    # 役割ごとにモデルを組み立て(設計判断 D-4)
    def _model(role: str) -> object:
        p = settings.provider_for_role(role, provider)
        return build_model(role, p, model_id=os.environ.get("BEDROCK_MODEL_ID", ""),
                           region=os.environ.get("BEDROCK_REGION", "us-west-2"))

    sim_manager = SIMManager(path=run_dir / "sim.json")
    loop = MainLoop(
        evaluator=Evaluator(model=_model("judge")),
        proposer=Proposer(model=_model("proposer"), sim_manager=sim_manager),
        selector=Selector(model=_model("selector"), diff_line_budget=settings.diff_line_budget),
        sim_manager=sim_manager,
        module_registry=ModuleRegistry(
            seed_dir=PROJECT_ROOT / "src" / "selfim" / "harness",
            workspace_dir=run_dir / "harness",
        ),
        gate=ValidationGate(diff_line_budget=settings.diff_line_budget),
        task_model=_model("task"),
        rubric=rubric,
        run_dir=run_dir,
        rollouts_per_task=settings.rollouts_per_task,
        suite_name=args.suite,
    )

    report: RunReport = loop.run(
        tasks=tasks, splits=(evolve, held_out), n_rounds=n_rounds, k_candidates=k_candidates
    )
    report.suite = args.suite
    report.provider = provider

    # レポート書き出し(splits も再現性のため記録)
    report_path = run_dir / "report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (run_dir / "splits.json").write_text(
        __import__("json").dumps({
            "evolve": [t.task_id for t in evolve],
            "held_out": [t.task_id for t in held_out],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[done] report -> %s", report_path)
    logger.info(
        "[done] held-out before=%.2f after=%.2f",
        report.held_out_before, report.held_out_after,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
