from __future__ import annotations

import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watermark_pipeline.config import PipelineConfig
from watermark_pipeline.process import run_command


LOGGER = logging.getLogger(__name__)
REQUIRED_PRODUCTION_PHASES = {"gen", "val", "gather", "issue"}


@dataclass(frozen=True)
class TaskProductionResult:
    task_file: Path
    evidence_file: Path | None = None


def produce_tasks(config: PipelineConfig, run_dir: Path) -> TaskProductionResult:
    tasks_dir = run_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    if config.swesmith.task_source in {"production", "modal"}:
        _require_full_production_phases(config)
        before_dir = tasks_dir / "production_before"
        after_dir = tasks_dir / "production_after"
        _download_tasks(config, before_dir)
        before_tasks = _load_task_candidates(
            before_dir,
            "**/*.json",
            require_problem_statement=False,
        )

        _run_generation(config)
        _download_tasks(config, after_dir)
        after_tasks = _load_task_candidates(
            after_dir,
            config.swesmith.task_file_glob,
            require_problem_statement=True,
        )

        before_ids = set(before_tasks)
        new_ids = sorted(set(after_tasks) - before_ids)
        if not new_ids:
            raise RuntimeError(
                "SWE-smith production completed without a new task instance; "
                "refusing to consume historical Modal volume data"
            )
        required_count = config.swesmith.required_task_count
        if required_count is not None and len(new_ids) < required_count:
            raise RuntimeError(
                "SWE-smith production yielded fewer new tasks than required: "
                f"required={required_count}, produced={len(new_ids)}"
            )
        if (
            required_count is not None
            and config.swesmith.task_limit is not None
            and config.swesmith.task_limit < required_count
        ):
            raise RuntimeError(
                "swesmith.task_limit cannot be smaller than required_task_count: "
                f"task_limit={config.swesmith.task_limit}, required={required_count}"
            )
        produced_tasks = [after_tasks[instance_id] for instance_id in new_ids]
        if config.swesmith.task_limit is not None:
            produced_tasks = produced_tasks[: config.swesmith.task_limit]
        if required_count is not None:
            produced_tasks = produced_tasks[:required_count]
        if not produced_tasks:
            raise RuntimeError("SWE-smith production did not yield an eligible task")

        merged_path = tasks_dir / "tasks.json"
        _write_tasks(merged_path, produced_tasks)
        evidence_path = tasks_dir / "task_production.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "producer": "SWE-smith",
                    "mode": "production",
                    "entrypoint": str(config.swesmith.script),
                    "command": [str(part) for part in _generation_command(config)],
                    "phases": _phase_list(config.swesmith.phases),
                    "repos": [repo.strip() for repo in config.swesmith.repos.split(",") if repo.strip()],
                    "language": config.swesmith.language,
                    "modal_volume": config.swesmith.modal_volume,
                    "remote_task_path": config.swesmith.task_remote_path,
                    "instances_before": len(before_ids),
                    "instances_after": len(after_tasks),
                    "required_task_count": required_count,
                    "new_instance_ids": new_ids,
                    "selected_instance_ids": [str(task["instance_id"]) for task in produced_tasks],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return TaskProductionResult(task_file=merged_path, evidence_file=evidence_path)
    elif config.swesmith.task_source == "local_static":
        source = config.swesmith.existing_task_file
        if source is None:
            raise RuntimeError("swesmith.existing_task_file is required for local_static task generation")
        raw_tasks = _merge_task_files([source])
        if config.swesmith.task_limit is not None:
            raw_tasks = raw_tasks[: config.swesmith.task_limit]
        raw_path = tasks_dir / "raw_tasks.json"
        raw_path.write_text(json.dumps(raw_tasks, indent=2, ensure_ascii=False), encoding="utf-8")
        run_command(
            [sys.executable, "swesmith/issue_gen/get_static.py", raw_path],
            cwd=config.swesmith.project_dir,
        )
        candidates = [raw_path.with_name(f"{raw_path.stem}__ig_static.json")]
    else:
        source = config.swesmith.existing_task_file
        if source is None:
            raise RuntimeError("swesmith.existing_task_file is required when generation is disabled")
        copied = tasks_dir / source.name
        shutil.copy2(source, copied)
        candidates = [copied]

    merged_path = tasks_dir / "tasks.json"
    tasks = _merge_task_files(candidates)
    if config.swesmith.task_limit is not None:
        tasks = tasks[: config.swesmith.task_limit]
    _write_tasks(merged_path, tasks)
    return TaskProductionResult(task_file=merged_path)


def _run_generation(config: PipelineConfig) -> None:
    run_command(_generation_command(config), cwd=config.swesmith.project_dir)


def _generation_command(config: PipelineConfig) -> list[str | Path]:
    return [
        config.swesmith.modal_executable,
        "run",
        config.swesmith.script,
        "--repos",
        config.swesmith.repos,
        "--language",
        config.swesmith.language,
        "--phases",
        config.swesmith.phases,
        "--max-bugs",
        str(config.swesmith.max_bugs),
        "--issue-gen-config",
        config.swesmith.issue_gen_config,
        "--issue-gen-workers",
        str(config.swesmith.issue_gen_workers),
    ]


def _require_full_production_phases(config: PipelineConfig) -> None:
    phases = set(_phase_list(config.swesmith.phases))
    missing = REQUIRED_PRODUCTION_PHASES - phases
    if missing:
        raise RuntimeError(
            "SWE-smith production mode requires gen,val,gather,issue; "
            f"missing: {','.join(sorted(missing))}"
        )


def _phase_list(phases: str) -> list[str]:
    return [phase.strip() for phase in phases.split(",") if phase.strip()]


def _download_tasks(config: PipelineConfig, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            config.swesmith.modal_executable,
            "volume",
            "get",
            config.swesmith.modal_volume,
            config.swesmith.task_remote_path,
            destination,
            "--force",
        ],
        cwd=config.swesmith.project_dir,
    )


def _load_task_candidates(
    root: Path,
    pattern: str,
    *,
    require_problem_statement: bool,
) -> dict[str, dict[str, Any]]:
    candidates = sorted(root.glob(pattern))
    if not candidates:
        LOGGER.info("No SWE-smith task artifacts matched %s below %s", pattern, root)
        return {}
    tasks: dict[str, dict[str, Any]] = {}
    for path in candidates:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for task in payload:
            if not isinstance(task, dict) or not _is_task(
                task,
                require_problem_statement=require_problem_statement,
            ):
                continue
            tasks[str(task["instance_id"])] = task
    return tasks


def _is_task(task: dict[str, Any], *, require_problem_statement: bool) -> bool:
    required = ["instance_id", "repo", "patch", "FAIL_TO_PASS", "image_name"]
    if require_problem_statement:
        required.append("problem_statement")
    return all(task.get(field) for field in required)


def _write_tasks(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")


def _merge_task_files(paths: list[Path]) -> list[dict[str, Any]]:
    instances: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for instance in payload:
            instances[str(instance["instance_id"])] = instance
    return list(instances.values())
