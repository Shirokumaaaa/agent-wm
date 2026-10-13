from __future__ import annotations

import json
import logging
import os
import re
import shlex
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

    if config.swesmith.task_source == "local_production":
        return _produce_local_tasks(config, run_dir, tasks_dir)
    elif config.swesmith.task_source in {"production", "modal"}:
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


def _produce_local_tasks(
    config: PipelineConfig,
    run_dir: Path,
    tasks_dir: Path,
) -> TaskProductionResult:
    smith = config.swesmith
    if not smith.local_profile:
        raise RuntimeError("swesmith.local_profile is required for local_production")
    if not smith.local_base_image:
        raise RuntimeError("swesmith.local_base_image is required for local_production")
    if not smith.local_task_repo:
        raise RuntimeError("swesmith.local_task_repo is required for local_production")
    if smith.local_issue_mode != "static":
        raise RuntimeError(
            "Only SWE-smith's static local issue generator is currently supported"
        )

    bug_dir = smith.project_dir / "logs" / "bug_gen" / smith.local_profile
    before_ids = _bug_artifact_ids(bug_dir, smith.local_profile)
    generation_command = _local_generation_command(config, run_dir)
    generation_env = dict(os.environ)
    generation_env["SWEFT_DOTENV_PATH"] = str(config.workspace_root / ".env")
    run_command(
        generation_command,
        cwd=smith.project_dir,
        env=generation_env,
    )

    after_ids = _bug_artifact_ids(bug_dir, smith.local_profile)
    new_ids = sorted(after_ids - before_ids)
    if not new_ids:
        raise RuntimeError(
            "SWE-smith local generation produced no new bug artifacts; "
            "refusing to reuse historical candidates"
        )

    collect_command: list[str | Path] = [
        sys.executable,
        "-m",
        "swesmith.bug_gen.collect_patches",
        Path("logs") / "bug_gen" / smith.local_profile,
    ]
    run_command(collect_command, cwd=smith.project_dir)
    collected_path = (
        smith.project_dir
        / "logs"
        / "bug_gen"
        / f"{smith.local_profile}_all_patches.json"
    )
    candidates = [
        item
        for item in _read_json_list(collected_path)
        if str(item.get("instance_id", "")) in set(new_ids)
    ]
    if not candidates:
        raise RuntimeError("SWE-smith did not collect any newly generated bug patches")

    candidate_path = tasks_dir / "local_candidates.json"
    _write_tasks(candidate_path, candidates)
    validation_command: list[str | Path] = [
        sys.executable,
        "-m",
        "swesmith.harness.valid",
        candidate_path,
        "--workers",
        str(smith.local_validation_workers),
    ]
    run_command(
        validation_command,
        cwd=smith.project_dir,
        group_wrapper=config.sweagent.docker_group if config.sweagent.docker_group_wrapper else None,
    )

    valid_tasks = _collect_locally_validated_tasks(config, candidates)
    required_count = smith.required_task_count
    if required_count is not None and len(valid_tasks) < required_count:
        raise RuntimeError(
            "SWE-smith local validation yielded fewer tasks than required: "
            f"required={required_count}, valid={len(valid_tasks)}"
        )
    if smith.task_limit is not None:
        valid_tasks = valid_tasks[: smith.task_limit]
    if required_count is not None:
        valid_tasks = valid_tasks[:required_count]
    if not valid_tasks:
        raise RuntimeError("SWE-smith local validation yielded no eligible task")

    image_name, image_command = _build_local_task_image(config, run_dir, valid_tasks)
    for task in valid_tasks:
        task["image_name"] = image_name

    gathered_path = tasks_dir / "local_gathered_tasks.json"
    _write_tasks(gathered_path, valid_tasks)
    issue_command: list[str | Path] = [
        sys.executable,
        "-m",
        "swesmith.issue_gen.get_static",
        gathered_path,
    ]
    run_command(issue_command, cwd=smith.project_dir)
    issue_path = gathered_path.with_name(f"{gathered_path.stem}__ig_static.json")
    produced_tasks = _read_json_list(issue_path)
    if len(produced_tasks) != len(valid_tasks):
        raise RuntimeError(
            "SWE-smith issue generation did not preserve the selected task count: "
            f"selected={len(valid_tasks)}, produced={len(produced_tasks)}"
        )
    if not all(_is_task(task, require_problem_statement=True) for task in produced_tasks):
        raise RuntimeError("SWE-smith local issue output contains incomplete tasks")

    merged_path = tasks_dir / "tasks.json"
    _write_tasks(merged_path, produced_tasks)
    evidence_path = tasks_dir / "task_production.json"
    evidence_path.write_text(
        json.dumps(
            {
                "producer": "SWE-smith",
                "mode": "local_production",
                "profile": smith.local_profile,
                "generator": smith.local_generator,
                "generation_command": [str(part) for part in generation_command],
                "collect_command": [str(part) for part in collect_command],
                "validation_command": [str(part) for part in validation_command],
                "local_image_command": [str(part) for part in image_command],
                "issue_command": [str(part) for part in issue_command],
                "bug_instances_before": len(before_ids),
                "bug_instances_after": len(after_ids),
                "new_bug_instance_ids": new_ids,
                "validated_instance_ids": [str(task["instance_id"]) for task in valid_tasks],
                "selected_instance_ids": [str(task["instance_id"]) for task in produced_tasks],
                "local_image": image_name,
                "required_task_count": required_count,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return TaskProductionResult(task_file=merged_path, evidence_file=evidence_path)


def _local_generation_command(config: PipelineConfig, run_dir: Path) -> list[str | Path]:
    smith = config.swesmith
    if smith.local_generator == "lm_modify":
        return [
            sys.executable,
            "-m",
            "swesmith.bug_gen.llm.modify",
            smith.local_profile,
            "--config_file",
            smith.local_generator_config,
            "--model",
            smith.local_generator_model,
            "--n_bugs",
            "1",
            "--max_bugs",
            str(smith.max_bugs),
            "--n_workers",
            str(smith.local_generation_workers),
        ]
    if smith.local_generator == "procedural":
        seed = sum(run_dir.name.encode("utf-8")) % (2**31)
        return [
            sys.executable,
            "-m",
            "swesmith.bug_gen.procedural.generate",
            smith.local_profile,
            "--max_bugs",
            str(smith.max_bugs),
            "--interleave",
            "--max_candidates",
            str(smith.max_bugs),
            "--seed",
            str(seed),
        ]
    raise RuntimeError(f"Unsupported local SWE-smith generator: {smith.local_generator}")


def _bug_artifact_ids(root: Path, profile: str) -> set[str]:
    if not root.exists():
        return set()
    return {
        f"{profile}.{path.stem.removeprefix('bug__')}"
        for path in root.glob("**/bug__*.diff")
    }


def _collect_locally_validated_tasks(
    config: PipelineConfig,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    smith = config.swesmith
    validation_root = smith.project_dir / "logs" / "run_validation" / smith.local_profile
    tasks: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: str(item["instance_id"])):
        instance_id = str(candidate["instance_id"])
        report_path = validation_root / instance_id / "report.json"
        patch_path = validation_root / instance_id / "patch.diff"
        if not report_path.is_file() or not patch_path.is_file():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        fail_to_pass = report.get("FAIL_TO_PASS", [])
        pass_to_pass = report.get("PASS_TO_PASS", [])
        if report.get("timed_out") or not fail_to_pass or not pass_to_pass:
            continue
        tasks.append(
            {
                **candidate,
                "instance_id": instance_id,
                "repo": smith.local_task_repo,
                "patch": patch_path.read_text(encoding="utf-8"),
                "FAIL_TO_PASS": fail_to_pass,
                "PASS_TO_PASS": pass_to_pass,
            }
        )
    return tasks


def _build_local_task_image(
    config: PipelineConfig,
    run_dir: Path,
    tasks: list[dict[str, Any]],
) -> tuple[str, list[str | Path]]:
    smith = config.swesmith
    build_dir = run_dir / "tasks" / "local_image"
    staging_dir = build_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    docker_lines = [
        f"FROM {smith.local_base_image}",
        "COPY staging /staging",
        "RUN set -eu; cd /testbed; "
        "git config user.email 'swesmith@swesmith.ai'; "
        "git config user.name 'swesmith'; "
        "git config commit.gpgsign false",
    ]
    for task in tasks:
        instance_id = str(task["instance_id"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", instance_id):
            raise RuntimeError(f"Unsafe SWE-smith instance ID: {instance_id}")
        task_dir = staging_dir / instance_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "patch.diff").write_text(str(task["patch"]), encoding="utf-8")
        test_files = sorted(
            {
                str(test).split("::", 1)[0]
                for test in task["FAIL_TO_PASS"]
                if _safe_relative_path(str(test).split("::", 1)[0])
            }
        )
        commands = [
            "set -eu",
            "cd /testbed",
            f"git checkout {shlex.quote(smith.local_base_branch)}",
            f"git reset --hard {shlex.quote(smith.local_base_branch)}",
            "git clean -fdq",
            f"git checkout -b {shlex.quote(instance_id)}",
            f"git apply {shlex.quote(f'/staging/{instance_id}/patch.diff')}",
            "git add -A",
            "git commit --no-gpg-sign -m 'Bug Patch'",
        ]
        if test_files:
            commands.extend(
                [
                    "git rm -f --ignore-unmatch -- "
                    + " ".join(shlex.quote(path) for path in test_files),
                    "git commit --no-gpg-sign -m 'Remove F2P Tests'",
                ]
            )
        docker_lines.append("RUN " + "; ".join(commands))
    docker_lines.append(
        "RUN set -eu; cd /testbed; "
        f"git checkout {shlex.quote(smith.local_base_branch)}; "
        f"git reset --hard {shlex.quote(smith.local_base_branch)}; git clean -fdq"
    )
    (build_dir / "Dockerfile").write_text(
        "\n".join(docker_lines) + "\n",
        encoding="utf-8",
    )

    base_repository = smith.local_base_image
    if ":" in base_repository.rsplit("/", 1)[-1]:
        base_repository = base_repository.rsplit(":", 1)[0]
    run_slug = re.sub(r"[^a-z0-9_.-]+", "-", run_dir.name.lower()).strip("-.")
    image_name = f"{base_repository}:{smith.local_image_tag_prefix}-{run_slug}"
    command: list[str | Path] = ["docker", "build", "-t", image_name, build_dir]
    run_command(
        command,
        cwd=config.workspace_root,
        group_wrapper=config.sweagent.docker_group if config.sweagent.docker_group_wrapper else None,
    )
    return image_name, command


def _safe_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError(f"Expected a JSON list of objects: {path}")
    return payload


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
