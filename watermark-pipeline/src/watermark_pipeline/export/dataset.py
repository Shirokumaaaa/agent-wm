from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from watermark_pipeline.schemas import SamplingDecision


def write_decisions(
    run_dir: Path,
    decisions: Iterable[SamplingDecision],
    *,
    algorithm: str,
    key_id: str,
) -> dict[str, int]:
    grouped: dict[str, list[dict]] = {
        "accepted": [],
        "rejected": [],
        "ineligible": [],
        "error": [],
    }
    for decision in decisions:
        grouped[decision.decision].append(decision.to_dict(algorithm=algorithm, key_id=key_id))

    for decision, records in grouped.items():
        path = run_dir / f"{decision}.json"
        path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    counts = {name: len(records) for name, records in grouped.items()}
    (run_dir / "run_report.json").write_text(
        json.dumps({"counts": counts}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return counts
