from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from watermark_pipeline.schemas import TerminalResponse


class IneligibleTrajectory(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def read_terminal_response(path: Path) -> TerminalResponse:
    data = json.loads(path.read_text(encoding="utf-8"))
    info = data.get("info", {})
    trajectory = data.get("trajectory", [])

    if info.get("exit_status") != "submitted":
        raise IneligibleTrajectory("agent_not_submitted")
    if not info.get("submission"):
        raise IneligibleTrajectory("no_patch")
    if not trajectory:
        raise IneligibleTrajectory("no_terminal_submit")

    final_step = trajectory[-1]
    if not _is_submit(final_step):
        raise IneligibleTrajectory("no_terminal_submit")
    response = str(final_step.get("response", "")).strip()
    if not response:
        raise IneligibleTrajectory("empty_response")
    return TerminalResponse(response=response, trajectory_path=path, trajectory_data=data)


def write_derived_trajectory(terminal: TerminalResponse, response: str, destination: Path) -> Path:
    data = copy.deepcopy(terminal.trajectory_data)
    final_step = data["trajectory"][-1]
    final_step["response"] = response
    final_step["thought"] = response

    for item in reversed(data.get("history", [])):
        if item.get("role") == "assistant" and _is_submit(item):
            item["content"] = response
            item["thought"] = response
            break

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination


def _is_submit(item: dict[str, Any]) -> bool:
    if str(item.get("action", "")).strip() == "submit":
        return True
    for call in item.get("tool_calls") or []:
        function = call.get("function", {})
        if function.get("name") == "submit":
            return True
    return False

