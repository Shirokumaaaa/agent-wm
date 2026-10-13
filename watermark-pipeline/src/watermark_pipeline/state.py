from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunState:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {
                "run_id": run_id,
                "created_at": _now(),
                "updated_at": _now(),
                "stages": {},
            }
            self.save()

    def stage_succeeded(self, name: str) -> bool:
        return self.data.get("stages", {}).get(name, {}).get("status") == "succeeded"

    def output(self, name: str, key: str) -> Any:
        return self.data["stages"][name]["outputs"][key]

    def start(self, name: str) -> None:
        self.data["stages"][name] = {
            "status": "running",
            "started_at": _now(),
            "outputs": {},
        }
        self.save()

    def succeed(self, name: str, outputs: dict[str, Any] | None = None) -> None:
        stage = self.data["stages"].setdefault(name, {})
        stage.update(
            {
                "status": "succeeded",
                "finished_at": _now(),
                "outputs": outputs or {},
            }
        )
        self.save()

    def fail(self, name: str, error: str) -> None:
        stage = self.data["stages"].setdefault(name, {})
        stage.update({"status": "failed", "finished_at": _now(), "error": error})
        self.save()

    def invalidate(self, name: str, reason: str) -> None:
        stage = self.data.get("stages", {}).get(name)
        if stage is None:
            return
        stage.update(
            {
                "status": "invalidated",
                "invalidated_at": _now(),
                "invalidation_reason": reason,
            }
        )
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
