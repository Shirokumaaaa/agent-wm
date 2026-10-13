from __future__ import annotations

import json
from pathlib import Path


def load_tasks(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_trajectory(trajectory_root: Path, instance_id: str) -> Path | None:
    expected = trajectory_root / instance_id / f"{instance_id}.traj"
    if expected.exists():
        return expected
    matches = list(trajectory_root.glob(f"**/{instance_id}.traj"))
    return matches[0] if matches else None

