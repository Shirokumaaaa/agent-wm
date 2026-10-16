from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from watermark_pipeline.config import PipelineConfig
from watermark_pipeline.adapters.swesmith import describe_generation_plan
from watermark_pipeline.orchestrator import PipelineRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the external terminal-response watermark pipeline.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Validate and print the SWE-smith generation plan without executing it.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = PipelineConfig.load(args.config)
    if args.plan_only:
        print(json.dumps(describe_generation_plan(config), indent=2, ensure_ascii=False))
        return
    run_dir = PipelineRunner(config, run_id=args.run_id).run()
    logging.getLogger(__name__).info("Pipeline output: %s", run_dir)
