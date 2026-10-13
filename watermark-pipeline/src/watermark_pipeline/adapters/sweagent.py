from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from watermark_pipeline.config import PipelineConfig
from watermark_pipeline.process import run_command


def run_batch(config: PipelineConfig, task_file: Path, run_dir: Path) -> Path:
    output_dir = run_dir / "trajectories"
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_config = _write_runtime_config(config, task_file, output_dir, run_dir)
    command: list[str | Path] = [
        config.sweagent.executable,
        "run-batch",
        "--config",
        runtime_config,
    ]
    run_command(
        command,
        cwd=config.sweagent.project_dir,
        group_wrapper=(config.sweagent.docker_group if config.sweagent.docker_group_wrapper else None),
    )
    validate_batch_outputs(task_file, output_dir)
    return output_dir


def validate_batch_outputs(
    task_file: Path,
    output_dir: Path,
    *,
    write_report: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    expected_ids = _load_expected_instance_ids(task_file, errors)
    statuses = _load_batch_statuses(output_dir, errors)

    expected_set = set(expected_ids)
    reported_set = set(statuses)
    missing_statuses = sorted(expected_set - reported_set)
    unexpected_statuses = sorted(reported_set - expected_set)
    if missing_statuses:
        errors.append(f"instances missing from batch status report: {', '.join(missing_statuses)}")
    if unexpected_statuses:
        errors.append(f"unexpected instances in batch status report: {', '.join(unexpected_statuses)}")

    uncaught = sorted(
        instance_id
        for instance_id, status in statuses.items()
        if status.casefold().startswith("uncaught ")
    )
    if uncaught:
        errors.append(f"instances ended with uncaught exceptions: {', '.join(uncaught)}")

    trajectory_files: dict[str, str] = {}
    for instance_id in expected_ids:
        trajectory_path = output_dir / instance_id / f"{instance_id}.traj"
        trajectory = _load_trajectory(trajectory_path, instance_id, errors)
        if trajectory is None:
            continue
        trajectory_files[instance_id] = str(trajectory_path.relative_to(output_dir))
        trajectory_status = trajectory.get("info", {}).get("exit_status")
        reported_status = statuses.get(instance_id)
        if reported_status is not None and not _status_matches(reported_status, trajectory_status):
            errors.append(
                f"exit status mismatch for {instance_id}: "
                f"report={reported_status!r}, trajectory={trajectory_status!r}"
            )

    report: dict[str, Any] = {
        "status": "failed" if errors else "succeeded",
        "expected_instance_ids": expected_ids,
        "instances_by_status": _group_statuses(statuses),
        "trajectory_files": trajectory_files,
        "errors": errors,
    }
    if write_report:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "batch_validation.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if errors:
        raise RuntimeError("SWE-agent batch output validation failed: " + "; ".join(errors))
    return report


def _load_expected_instance_ids(task_file: Path, errors: list[str]) -> list[str]:
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
    except Exception as error:
        errors.append(f"cannot read task file {task_file}: {error}")
        return []
    if not isinstance(payload, list) or not payload:
        errors.append("task file must contain a non-empty JSON array")
        return []
    instance_ids: list[str] = []
    for index, task in enumerate(payload):
        if not isinstance(task, dict) or not task.get("instance_id"):
            errors.append(f"task at index {index} has no instance_id")
            continue
        instance_ids.append(str(task["instance_id"]))
    duplicates = sorted({item for item in instance_ids if instance_ids.count(item) > 1})
    if duplicates:
        errors.append(f"duplicate task instance IDs: {', '.join(duplicates)}")
    return list(dict.fromkeys(instance_ids))


def _load_batch_statuses(output_dir: Path, errors: list[str]) -> dict[str, str]:
    status_path = output_dir / "run_batch_exit_statuses.yaml"
    if not status_path.is_file():
        errors.append(f"batch status report not found: {status_path}")
        return {}
    try:
        payload = yaml.safe_load(status_path.read_text(encoding="utf-8"))
    except Exception as error:
        errors.append(f"cannot parse batch status report {status_path}: {error}")
        return {}
    grouped = payload.get("instances_by_exit_status") if isinstance(payload, dict) else None
    if not isinstance(grouped, dict):
        errors.append("batch status report has no instances_by_exit_status mapping")
        return {}
    statuses: dict[str, str] = {}
    for raw_status, raw_instances in grouped.items():
        status = str(raw_status) if raw_status is not None else "unknown_exit"
        if not isinstance(raw_instances, list):
            errors.append(f"batch status {status!r} does not contain an instance list")
            continue
        for raw_instance_id in raw_instances:
            instance_id = str(raw_instance_id)
            if instance_id in statuses:
                errors.append(f"instance appears under multiple batch statuses: {instance_id}")
                continue
            statuses[instance_id] = status
    return statuses


def _load_trajectory(
    trajectory_path: Path,
    instance_id: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not trajectory_path.is_file():
        errors.append(f"trajectory not found for {instance_id}: {trajectory_path}")
        return None
    try:
        payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except Exception as error:
        errors.append(f"cannot parse trajectory for {instance_id}: {error}")
        return None
    if not isinstance(payload, dict):
        errors.append(f"trajectory for {instance_id} is not a JSON object")
        return None
    if payload.get("environment") != instance_id:
        errors.append(
            f"trajectory identity mismatch for {instance_id}: environment={payload.get('environment')!r}"
        )
    info = payload.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("exit_status"), str):
        errors.append(f"trajectory for {instance_id} has no string info.exit_status")
    if not isinstance(payload.get("trajectory"), list):
        errors.append(f"trajectory for {instance_id} has no trajectory list")
    return payload


def _status_matches(reported_status: str, trajectory_status: Any) -> bool:
    if not isinstance(trajectory_status, str):
        return False
    if reported_status == trajectory_status:
        return True
    return reported_status == f"skipped ({trajectory_status})"


def _group_statuses(statuses: dict[str, str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for instance_id, status in sorted(statuses.items()):
        grouped.setdefault(status, []).append(instance_id)
    return grouped


def _write_runtime_config(
    config: PipelineConfig,
    task_file: Path,
    output_dir: Path,
    run_dir: Path,
) -> Path:
    data = yaml.safe_load(config.sweagent.config.read_text(encoding="utf-8"))
    data["instances"]["path"] = str(task_file)
    data["output_dir"] = str(output_dir)

    proxy_url = config.sweagent.container_proxy_url
    if proxy_url:
        docker_args = list(data["instances"]["deployment"].get("docker_args", []))
        proxy_args = [
            "--add-host=host.docker.internal:host-gateway",
            f"--env=HTTP_PROXY={proxy_url}",
            f"--env=HTTPS_PROXY={proxy_url}",
            f"--env=http_proxy={proxy_url}",
            f"--env=https_proxy={proxy_url}",
            "--env=NO_PROXY=localhost,127.0.0.1",
        ]
        for argument in proxy_args:
            if argument not in docker_args:
                docker_args.append(argument)
        data["instances"]["deployment"]["docker_args"] = docker_args

    runtime_config = run_dir / "sweagent.runtime.yaml"
    runtime_config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return runtime_config
