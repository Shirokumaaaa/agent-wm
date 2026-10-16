from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from watermark_pipeline.config import (
    CombinerConfig,
    GeneratorConfig,
    LocalRepositoryConfig,
    PipelineConfig,
    SWESmithConfig,
)
from watermark_pipeline.process import run_command


LOGGER = logging.getLogger(__name__)
REQUIRED_PRODUCTION_PHASES = {"gen", "val", "gather", "issue"}


@dataclass(frozen=True)
class TaskProductionResult:
    task_file: Path
    evidence_file: Path | None = None


class NoMirrorInputs(RuntimeError):
    """The configured mirror dataset has no records for a selected repository."""


def describe_generation_plan(config: PipelineConfig) -> dict[str, Any]:
    """Validate and render the local SWE-smith plan without executing it."""
    smith = config.swesmith
    if smith.task_source != "local_production":
        return {"task_source": smith.task_source, "local_plan": None}
    if not 1 <= smith.local_repository_limit <= len(smith.local_repositories):
        raise RuntimeError(
            "swesmith.local_repository_limit must be between 1 and the number "
            f"of configured repositories ({len(smith.local_repositories)})"
        )
    if smith.local_repository_workers <= 0:
        raise RuntimeError("swesmith.local_repository_workers must be positive")
    repositories = smith.local_repositories[: smith.local_repository_limit]
    rendered_generators = []
    for generator in smith.generators:
        if generator.config_file:
            config_file = Path(generator.config_file)
            if not config_file.is_absolute():
                config_file = smith.project_dir / config_file
            if not config_file.is_file():
                raise RuntimeError(
                    f"Generator {generator.id!r} config file does not exist: {config_file}"
                )
        for input_file in generator.input_files:
            if not input_file.is_file():
                raise RuntimeError(
                    f"Generator {generator.id!r} input file does not exist: {input_file}"
                )
        allocations = _distribute_budget(generator.budget, len(repositories))
        rendered_generators.append(
            {
                "id": generator.id,
                "kind": generator.kind,
                "budget": generator.budget,
                "allocations": {
                    repository.profile: budget
                    for repository, budget in zip(
                        repositories, allocations, strict=True
                    )
                },
                "model": generator.model,
                "workers": generator.workers,
                "input_files": [str(path) for path in generator.input_files],
            }
        )
    return {
        "task_source": smith.task_source,
        "repositories": [repository.profile for repository in repositories],
        "repository_workers": smith.local_repository_workers,
        "generators": rendered_generators,
        "combiners": [
            {
                "id": combiner.id,
                "kind": combiner.kind,
                "sources": list(combiner.sources),
                "num_patches": combiner.num_patches,
                "limit": combiner.limit,
            }
            for combiner in smith.combiners
        ],
        "issue_modes": {
            "default": smith.local_issue_mode,
            "mirror": smith.mirror_issue_mode,
            "mirror_fallback": smith.mirror_issue_fallback,
        },
    }


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
                    "repos": [
                        repo.strip()
                        for repo in config.swesmith.repos.split(",")
                        if repo.strip()
                    ],
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
            raise RuntimeError(
                "swesmith.existing_task_file is required for local_static "
                "task generation"
            )
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
            raise RuntimeError(
                "swesmith.existing_task_file is required when generation is disabled"
            )
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
    repositories = smith.local_repositories
    if not repositories:
        raise RuntimeError(
            "swesmith.local_repositories is required for local_production"
        )
    if not 1 <= smith.local_repository_limit <= len(repositories):
        raise RuntimeError(
            "swesmith.local_repository_limit must be between 1 and the number "
            f"of configured repositories ({len(repositories)})"
        )
    if smith.local_repository_workers <= 0:
        raise RuntimeError("swesmith.local_repository_workers must be positive")
    repositories = repositories[: smith.local_repository_limit]
    for generator in smith.generators:
        if generator.kind != "mirror" and generator.budget < len(repositories):
            raise RuntimeError(
                f"Generator {generator.id!r} budget must be at least "
                "local_repository_limit so every selected repository receives work"
            )
    required_count = smith.required_task_count
    if (
        required_count is not None
        and smith.task_limit is not None
        and smith.task_limit < required_count
    ):
        raise RuntimeError(
            "swesmith.task_limit cannot be smaller than required_task_count: "
            f"task_limit={smith.task_limit}, required={required_count}"
        )
    if smith.local_issue_mode not in {"static", "llm"}:
        raise RuntimeError(
            "Unsupported swesmith.local_issue_mode: "
            f"{smith.local_issue_mode} (expected 'static' or 'llm')"
        )
    if smith.mirror_issue_mode not in {"original_pr", "static", "llm"}:
        raise RuntimeError(
            "Unsupported swesmith.mirror_issue_mode: "
            f"{smith.mirror_issue_mode} (expected 'original_pr', 'static', or 'llm')"
        )
    if smith.mirror_issue_fallback not in {"static", "llm"}:
        raise RuntimeError(
            "Unsupported swesmith.mirror_issue_fallback: "
            f"{smith.mirror_issue_fallback} (expected 'static' or 'llm')"
        )

    generator_budgets = {
        generator.id: _distribute_budget(generator.budget, len(repositories))
        for generator in smith.generators
    }
    task_groups: list[list[dict[str, Any]]] = [[] for _ in repositories]
    evidence_path = tasks_dir / "task_production.json"
    previous_repositories: dict[str, dict[str, Any]] = {}
    if evidence_path.is_file():
        previous_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        previous_repositories = {
            str(item.get("profile")): item
            for item in previous_payload.get("repositories", [])
        }
    evidence_payload: dict[str, Any] = {
        "producer": "SWE-smith",
        "mode": "local_production",
        "status": "running",
        "generators": [generator.id for generator in smith.generators],
        "combiners": [combiner.id for combiner in smith.combiners],
        "local_issue_mode": smith.local_issue_mode,
        "repository_limit": smith.local_repository_limit,
        "repository_workers": min(smith.local_repository_workers, len(repositories)),
        "planned_bug_attempts": sum(generator.budget for generator in smith.generators),
        "required_task_count": required_count,
        "selected_instance_ids": [],
        "repositories": [
            _new_repository_evidence(
                repository,
                sum(values[repository_index] for values in generator_budgets.values()),
            )
            for repository_index, repository in enumerate(repositories)
        ],
    }
    evidence_lock = Lock()
    _write_production_evidence(evidence_path, evidence_payload)

    def produce_repository(repository_index: int) -> None:
        repository = repositories[repository_index]
        budgets = {
            generator_id: values[repository_index]
            for generator_id, values in generator_budgets.items()
        }
        repository_evidence = evidence_payload["repositories"][repository_index]

        def persist_evidence() -> None:
            with evidence_lock:
                _write_production_evidence(evidence_path, evidence_payload)

        try:
            tasks = _produce_local_repository_tasks(
                config,
                run_dir,
                tasks_dir,
                repository,
                budgets,
                previous_repositories.get(repository.profile, {}),
                repository_evidence,
                persist_evidence,
            )
        except Exception as error:
            LOGGER.exception(
                "Local SWE-smith production failed for %s",
                repository.profile,
            )
            repository_evidence["status"] = "failed"
            repository_evidence["error"] = f"{type(error).__name__}: {error}"
            _fail_started_attempt_stages(repository_evidence)
        else:
            task_groups[repository_index] = tasks
            repository_evidence["status"] = (
                "completed_with_failures"
                if _repository_has_failures(repository_evidence)
                else "succeeded"
            )
        persist_evidence()

    worker_count = min(smith.local_repository_workers, len(repositories))
    LOGGER.info(
        "Starting local SWE-smith production for %d repositories with %d repository workers",
        len(repositories),
        worker_count,
    )
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="swesmith-repository",
    ) as executor:
        futures = [
            executor.submit(produce_repository, repository_index)
            for repository_index in range(len(repositories))
        ]
        for future in as_completed(futures):
            future.result()

    producer_order = [generator.id for generator in smith.generators] + [
        combiner.id for combiner in smith.combiners
    ]
    produced_tasks = _round_robin_by_producer(task_groups, producer_order)
    if smith.task_limit is not None:
        produced_tasks = produced_tasks[: smith.task_limit]
    selected_count = required_count if required_count is not None else len(produced_tasks)
    produced_tasks = produced_tasks[:selected_count]
    for task in produced_tasks:
        task["bug_generator"] = task.pop("_wm_generator_id", "unknown")
    selected_ids = {str(task["instance_id"]) for task in produced_tasks}
    for repository_evidence in evidence_payload["repositories"]:
        for artifact in repository_evidence.get("artifacts", []):
            artifact["selected_for_agent"] = artifact.get("instance_id") in selected_ids

    merged_path = tasks_dir / "tasks.json"
    _write_tasks(merged_path, produced_tasks)
    evidence_payload["selected_instance_ids"] = sorted(selected_ids)
    if required_count is not None and len(produced_tasks) < required_count:
        evidence_payload["status"] = "failed"
        _write_production_evidence(evidence_path, evidence_payload)
        raise RuntimeError(
            "SWE-smith multi-repository production yielded fewer tasks than required: "
            f"required={required_count}, produced={len(produced_tasks)}; "
            f"see {evidence_path}"
        )
    if not produced_tasks:
        evidence_payload["status"] = "failed"
        _write_production_evidence(evidence_path, evidence_payload)
        raise RuntimeError(
            "SWE-smith multi-repository production yielded no eligible task; "
            f"see {evidence_path}"
        )
    evidence_payload["status"] = (
        "completed_with_failures"
        if any(
            repository["status"] != "succeeded"
            for repository in evidence_payload["repositories"]
        )
        else "succeeded"
    )
    _write_production_evidence(evidence_path, evidence_payload)
    return TaskProductionResult(task_file=merged_path, evidence_file=evidence_path)


def _produce_local_repository_tasks(
    config: PipelineConfig,
    run_dir: Path,
    tasks_dir: Path,
    repository: LocalRepositoryConfig,
    generator_budgets: dict[str, int],
    previous_evidence: dict[str, Any],
    evidence: dict[str, Any],
    persist_evidence: Callable[[], None],
) -> list[dict[str, Any]]:
    smith = config.swesmith
    repository_dir = tasks_dir / "repositories" / _slug(repository.profile)
    repository_dir.mkdir(parents=True, exist_ok=True)
    bug_dir = smith.project_dir / "logs" / "bug_gen" / repository.profile
    generation_env = dict(os.environ)
    generation_env["SWEFT_DOTENV_PATH"] = str(config.workspace_root / ".env")
    evidence["status"] = "running"
    previous_jobs = {
        (str(job.get("stage")), str(job.get("id"))): job
        for job in previous_evidence.get("jobs", [])
    }
    generated_ids: dict[str, str] = {}
    for generator in smith.generators:
        budget = generator_budgets[generator.id]
        if budget <= 0:
            continue
        before_ids = _bug_artifact_ids(bug_dir, repository.profile)
        job = {
            "id": generator.id,
            "kind": generator.kind,
            "stage": "generation",
            "budget": budget,
            "status": "scheduled",
            "artifact_instance_ids": [],
        }
        evidence["jobs"].append(job)
        previous_job = previous_jobs.get(("generation", generator.id), {})
        reusable_ids = sorted(
            set(
                previous_job.get("artifact_instance_ids", [])
                if previous_job.get("kind") == generator.kind
                else []
            )
            & before_ids
        )[:budget]
        if reusable_ids:
            job["artifact_instance_ids"] = reusable_ids
            job["status"] = "reused_from_interrupted_run"
            generated_ids.update(
                {instance_id: generator.id for instance_id in reusable_ids}
            )
            persist_evidence()
            continue
        try:
            command = _local_generator_command(
                config, run_dir, repository_dir, repository, generator, budget
            )
        except NoMirrorInputs as error:
            job["status"] = "skipped_no_inputs"
            job["reason"] = str(error)
            persist_evidence()
            continue
        job["command"] = [str(part) for part in command]
        job["status"] = "running"
        persist_evidence()
        try:
            run_command(command, cwd=smith.project_dir, env=generation_env)
        except Exception as error:
            LOGGER.exception(
                "SWE-smith generator %s failed for %s; preserving other generators",
                generator.id,
                repository.profile,
            )
            new_ids = sorted(
                _bug_artifact_ids(bug_dir, repository.profile) - before_ids
            )[:budget]
            job["artifact_instance_ids"] = new_ids
            job["status"] = "failed_with_artifacts" if new_ids else "failed"
            job["error"] = f"{type(error).__name__}: {error}"
        else:
            after_ids = _bug_artifact_ids(bug_dir, repository.profile)
            new_ids = sorted(after_ids - before_ids)[:budget]
            job["artifact_instance_ids"] = new_ids
            job["status"] = "succeeded" if new_ids else "completed_empty"
        generated_ids.update({instance_id: generator.id for instance_id in new_ids})
        persist_evidence()

    if not generated_ids:
        raise RuntimeError(
            "SWE-smith direct generators produced no new bug artifacts; "
            "refusing to reuse historical candidates"
        )

    direct_candidates = _collect_candidates(
        config, repository, repository_dir, generated_ids, "direct"
    )
    direct_valid = _validate_candidates(
        config, repository, repository_dir, direct_candidates, "direct", evidence
    )
    persist_evidence()

    combined_valid: list[dict[str, Any]] = []
    for combiner in smith.combiners:
        source_tasks = [
            task
            for task in direct_valid
            if task.get("_wm_generator_id") in set(combiner.sources)
        ]
        job = {
            "id": combiner.id,
            "kind": combiner.kind,
            "stage": "combine",
            "sources": list(combiner.sources),
            "status": "scheduled",
            "artifact_instance_ids": [],
        }
        evidence["jobs"].append(job)
        if len(source_tasks) < combiner.num_patches:
            job["status"] = "skipped_insufficient_sources"
            persist_evidence()
            continue
        source_path = repository_dir / f"{combiner.id}_validated_sources.json"
        _write_tasks(source_path, source_tasks)
        before_ids = _bug_artifact_ids(bug_dir, repository.profile)
        previous_job = previous_jobs.get(("combine", combiner.id), {})
        reusable_ids = sorted(
            set(
                previous_job.get("artifact_instance_ids", [])
                if previous_job.get("kind") == combiner.kind
                else []
            )
            & before_ids
        )
        if combiner.limit > 0:
            reusable_ids = reusable_ids[: combiner.limit]
        if reusable_ids:
            new_ids = reusable_ids
            job["artifact_instance_ids"] = new_ids
            job["status"] = "reused_from_interrupted_run"
        else:
            command = _local_combiner_command(repository, combiner, source_path)
            job["command"] = [str(part) for part in command]
            job["status"] = "running"
            persist_evidence()
            try:
                run_command(command, cwd=smith.project_dir, env=generation_env)
            except Exception as error:
                LOGGER.exception(
                    "SWE-smith combiner %s failed for %s; preserving validated tasks",
                    combiner.id,
                    repository.profile,
                )
                new_ids = sorted(
                    _bug_artifact_ids(bug_dir, repository.profile) - before_ids
                )
                if combiner.limit > 0:
                    new_ids = new_ids[: combiner.limit]
                job["artifact_instance_ids"] = new_ids
                job["status"] = "failed_with_artifacts" if new_ids else "failed"
                job["error"] = f"{type(error).__name__}: {error}"
                generated_ids.update(
                    {instance_id: combiner.id for instance_id in new_ids}
                )
                persist_evidence()
                continue
            else:
                new_ids = sorted(
                    _bug_artifact_ids(bug_dir, repository.profile) - before_ids
                )
                if combiner.limit > 0:
                    new_ids = new_ids[: combiner.limit]
                job["artifact_instance_ids"] = new_ids
                job["status"] = "succeeded" if new_ids else "completed_empty"
        persist_evidence()
        if not new_ids:
            continue
        combined_candidates = _collect_candidates(
            config,
            repository,
            repository_dir,
            {instance_id: combiner.id for instance_id in new_ids},
            combiner.id,
        )
        combined_valid.extend(
            _validate_candidates(
                config,
                repository,
                repository_dir,
                combined_candidates,
                combiner.id,
                evidence,
            )
        )
        persist_evidence()

    valid_tasks = direct_valid + combined_valid
    if not valid_tasks:
        raise RuntimeError("SWE-smith local validation yielded no eligible task")

    produced_tasks, issue_commands = _generate_local_issues(
        config, repository_dir, valid_tasks, generation_env
    )
    evidence["issue_commands"] = issue_commands
    produced_ids = {str(task["instance_id"]) for task in produced_tasks}
    if not produced_tasks:
        raise RuntimeError("SWE-smith issue generation did not produce any task instance")
    if len(produced_tasks) != len(valid_tasks):
        LOGGER.warning(
            "SWE-smith issue generation kept %d of %d task instances for %s",
            len(produced_tasks),
            len(valid_tasks),
            repository.profile,
        )
    image_name, image_command = _build_local_task_image(
        config,
        run_dir,
        repository,
        produced_tasks,
    )
    evidence["local_image_command"] = [str(part) for part in image_command]
    evidence["local_image"] = image_name
    for task in produced_tasks:
        task["image_name"] = image_name
    if not all(_is_task(task, require_problem_statement=True) for task in produced_tasks):
        raise RuntimeError("SWE-smith local issue output contains incomplete tasks")

    valid_ids = {str(task["instance_id"]) for task in valid_tasks}
    producer_by_id = {
        instance_id: job["id"]
        for job in evidence["jobs"]
        for instance_id in job.get("artifact_instance_ids", [])
    }
    all_artifact_ids = set(producer_by_id)
    valid_by_id = {str(task["instance_id"]): task for task in valid_tasks}
    evidence["artifacts"] = [
        {
            "instance_id": instance_id,
            "producer_id": producer_by_id.get(instance_id),
            "parent_instance_ids": _parent_instance_ids(
                repository.profile, valid_by_id.get(instance_id, {})
            ),
            "validated": instance_id in valid_ids,
            "issue_generated": instance_id in produced_ids,
            "selected_for_agent": False,
        }
        for instance_id in sorted(all_artifact_ids)
    ]
    evidence["new_bug_instance_ids"] = sorted(all_artifact_ids)
    evidence["validated_instance_ids"] = sorted(valid_ids)
    evidence["produced_instance_ids"] = sorted(produced_ids)
    evidence["local_image_command"] = [str(part) for part in image_command]
    evidence["local_image"] = image_name
    persist_evidence()
    return produced_tasks


def _parent_instance_ids(profile: str, task: dict[str, Any]) -> list[str]:
    parents = []
    for value in task.get("patch_files", []):
        artifact = Path(str(value)).name.removesuffix(".diff").removeprefix("bug__")
        parents.append(f"{profile}.{artifact}")
    return parents


def _local_generator_command(
    config: PipelineConfig,
    run_dir: Path,
    repository_dir: Path,
    repository: LocalRepositoryConfig,
    generator: GeneratorConfig,
    bug_budget: int,
) -> list[str | Path]:
    if generator.kind == "lm_modify":
        return [
            sys.executable,
            "-m",
            "swesmith.bug_gen.llm.modify",
            repository.profile,
            "--config_file",
            str(generator.config_file),
            "--model",
            str(generator.model),
            "--n_bugs",
            "1",
            "--max_bugs",
            str(bug_budget),
            "--n_workers",
            str(generator.workers),
        ]
    if generator.kind == "lm_rewrite":
        return [
            sys.executable,
            "-m",
            "swesmith.bug_gen.llm.rewrite",
            repository.profile,
            "--config_file",
            str(generator.config_file),
            "--model",
            str(generator.model),
            "--max_bugs",
            str(bug_budget),
            "--n_workers",
            str(generator.workers),
        ]
    if generator.kind == "procedural":
        seed_material = f"{run_dir.name}:{repository.profile}:{generator.id}"
        seed = sum(seed_material.encode("utf-8")) % (2**31)
        return [
            sys.executable,
            "-m",
            "swesmith.bug_gen.procedural.generate",
            repository.profile,
            "--max_bugs",
            str(bug_budget),
            "--interleave",
            "--max_candidates",
            str(bug_budget),
            "--seed",
            str(seed),
        ]
    if generator.kind == "mirror":
        input_path, profile_map_path = _prepare_mirror_input(
            repository_dir, repository, generator, bug_budget
        )
        return [
            sys.executable,
            "-m",
            "swesmith.bug_gen.mirror.generate",
            input_path,
            "--model",
            str(generator.model),
            "--num_processes",
            str(generator.workers),
            "--max_files",
            str(generator.max_files),
            "--max_lines",
            str(generator.max_lines),
            "--max_file_lines",
            str(generator.max_file_lines),
            "--profile-map",
            profile_map_path,
        ]
    raise RuntimeError(f"Unsupported local SWE-smith generator: {generator.kind}")


def _prepare_mirror_input(
    repository_dir: Path,
    repository: LocalRepositoryConfig,
    generator: GeneratorConfig,
    budget: int,
) -> tuple[Path, Path]:
    owner, repository_revision = repository.profile.split("__", 1)
    repository_name = repository_revision.rsplit(".", 1)[0]
    source_repo = f"{owner}/{repository_name}"
    records: list[dict[str, Any]] = []
    for path in generator.input_files:
        if not path.is_file():
            raise RuntimeError(f"Mirror input file does not exist: {path}")
        if path.name.endswith((".jsonl", ".jsonl.all")):
            values = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            values = _read_json_list(path)
        records.extend(item for item in values if item.get("repo") == source_repo)
    records = records[:budget]
    if not records:
        raise NoMirrorInputs(
            f"Mirror generator {generator.id!r} has no inputs for {source_repo}"
        )
    input_path = repository_dir / f"{generator.id}_mirror_inputs.json"
    profile_map_path = repository_dir / f"{generator.id}_profile_map.json"
    _write_tasks(input_path, records)
    profile_map_path.write_text(
        json.dumps({source_repo: repository.profile}, indent=2), encoding="utf-8"
    )
    return input_path, profile_map_path


def _local_combiner_command(
    repository: LocalRepositoryConfig,
    combiner: CombinerConfig,
    validated_instances_file: Path,
) -> list[str | Path]:
    suffix = "file" if combiner.kind == "combine_file" else "module"
    module = f"swesmith.bug_gen.combine.same_{suffix}"
    command: list[str | Path] = [
        sys.executable,
        "-m",
        module,
        Path("logs") / "bug_gen" / repository.profile,
        "--num_patches",
        str(combiner.num_patches),
        "--max_combos",
        str(combiner.max_combos),
        "--validated-instances-file",
        validated_instances_file,
    ]
    if combiner.kind == "combine_file":
        command.extend(["--limit_per_file", str(combiner.limit)])
    else:
        command.extend(
            ["--limit_per_module", str(combiner.limit), "--depth", str(combiner.depth)]
        )
    return command


def _collect_candidates(
    config: PipelineConfig,
    repository: LocalRepositoryConfig,
    repository_dir: Path,
    producer_ids: dict[str, str],
    label: str,
) -> list[dict[str, Any]]:
    collect_command: list[str | Path] = [
        sys.executable,
        "-m",
        "swesmith.bug_gen.collect_patches",
        Path("logs") / "bug_gen" / repository.profile,
    ]
    run_command(collect_command, cwd=config.swesmith.project_dir)
    collected_path = (
        config.swesmith.project_dir
        / "logs"
        / "bug_gen"
        / f"{repository.profile}_all_patches.json"
    )
    candidates = [
        {**item, "_wm_generator_id": producer_ids[str(item["instance_id"])]}
        for item in _read_json_list(collected_path)
        if str(item.get("instance_id", "")) in producer_ids
    ]
    if producer_ids and not candidates:
        raise RuntimeError(f"SWE-smith did not collect any {label} bug patches")
    _write_tasks(repository_dir / f"{label}_candidates.json", candidates)
    return candidates


def _validate_candidates(
    config: PipelineConfig,
    repository: LocalRepositoryConfig,
    repository_dir: Path,
    candidates: list[dict[str, Any]],
    label: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    candidate_path = repository_dir / f"{label}_candidates.json"
    _write_tasks(candidate_path, candidates)
    command: list[str | Path] = [
        sys.executable,
        "-m",
        "swesmith.harness.valid",
        candidate_path,
        "--workers",
        str(config.swesmith.local_validation_workers),
    ]
    evidence["validation_commands"].append([str(part) for part in command])
    run_command(
        command,
        cwd=config.swesmith.project_dir,
        group_wrapper=(
            config.sweagent.docker_group
            if config.sweagent.docker_group_wrapper
            else None
        ),
    )
    return _collect_locally_validated_tasks(config, repository, candidates)


def _generate_local_issues(
    config: PipelineConfig,
    repository_dir: Path,
    valid_tasks: list[dict[str, Any]],
    env: dict[str, str],
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    mirror_ids = {
        generator.id
        for generator in config.swesmith.generators
        if generator.kind == "mirror"
    }
    standard_tasks = [
        task
        for task in valid_tasks
        if task.get("_wm_generator_id") not in mirror_ids
    ]
    mirror_tasks = [
        task
        for task in valid_tasks
        if task.get("_wm_generator_id") in mirror_ids
    ]
    groups = [
        ("standard", config.swesmith.local_issue_mode, standard_tasks),
        ("mirror", config.swesmith.mirror_issue_mode, mirror_tasks),
    ]
    produced: list[dict[str, Any]] = []
    commands: list[list[str]] = []
    for label, mode, tasks in groups:
        if not tasks:
            continue
        gathered_path = repository_dir / f"{label}_gathered_tasks.json"
        _write_tasks(gathered_path, tasks)
        command = _local_issue_command(config, gathered_path, mode=mode)
        commands.append([str(part) for part in command])
        run_command(command, cwd=config.swesmith.project_dir, env=env)
        output_path = _local_issue_output_path(config.swesmith, gathered_path, mode=mode)
        generated = _read_json_list(output_path) if output_path.is_file() else []
        if mode == "original_pr":
            generated_ids = {str(task["instance_id"]) for task in generated}
            missing = [task for task in tasks if str(task["instance_id"]) not in generated_ids]
            if missing:
                fallback_path = repository_dir / f"{label}_fallback_tasks.json"
                _write_tasks(fallback_path, missing)
                fallback = config.swesmith.mirror_issue_fallback
                fallback_command = _local_issue_command(config, fallback_path, mode=fallback)
                commands.append([str(part) for part in fallback_command])
                run_command(fallback_command, cwd=config.swesmith.project_dir, env=env)
                generated.extend(
                    _read_json_list(
                        _local_issue_output_path(config.swesmith, fallback_path, mode=fallback)
                    )
                )
        produced.extend(generated)
    return produced, commands


def _local_issue_command(
    config: PipelineConfig, gathered_path: Path, *, mode: str | None = None
) -> list[str | Path]:
    smith = config.swesmith
    mode = mode or smith.local_issue_mode
    if mode == "static":
        return [sys.executable, "-m", "swesmith.issue_gen.get_static", gathered_path]
    if mode == "original_pr":
        return [sys.executable, "-m", "swesmith.issue_gen.get_from_pr", gathered_path]
    issue_config = Path(smith.issue_gen_config)
    if not issue_config.is_absolute():
        issue_config = config.workspace_root / issue_config
    return [
        sys.executable,
        "-m",
        "swesmith.issue_gen.generate",
        "-d",
        gathered_path,
        "-c",
        issue_config,
        "-w",
        str(smith.issue_gen_workers),
        "-r",
    ]


def _local_issue_output_path(
    smith: SWESmithConfig, gathered_path: Path, *, mode: str | None = None
) -> Path:
    mode = mode or smith.local_issue_mode
    suffix = {"static": "__ig_static", "llm": "__ig_llm", "original_pr": "__ig_orig"}[mode]
    return gathered_path.with_name(f"{gathered_path.stem}{suffix}.json")


def _bug_artifact_ids(root: Path, profile: str) -> set[str]:
    if not root.exists():
        return set()
    return {
        f"{profile}.{path.stem.removeprefix('bug__')}"
        for path in root.glob("**/bug__*.diff")
    }


def _collect_locally_validated_tasks(
    config: PipelineConfig,
    repository: LocalRepositoryConfig,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    smith = config.swesmith
    validation_root = (
        smith.project_dir / "logs" / "run_validation" / repository.profile
    )
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
                "repo": repository.task_repo,
                "patch": patch_path.read_text(encoding="utf-8"),
                "FAIL_TO_PASS": fail_to_pass,
                "PASS_TO_PASS": pass_to_pass,
            }
        )
    return tasks


def _build_local_task_image(
    config: PipelineConfig,
    run_dir: Path,
    repository: LocalRepositoryConfig,
    tasks: list[dict[str, Any]],
) -> tuple[str, list[str | Path]]:
    build_dir = run_dir / "tasks" / "local_images" / _slug(repository.profile)
    staging_dir = build_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    docker_lines = [
        f"FROM {repository.base_image}",
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
            f"git checkout {shlex.quote(repository.base_branch)}",
            f"git reset --hard {shlex.quote(repository.base_branch)}",
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
        f"git checkout {shlex.quote(repository.base_branch)}; "
        f"git reset --hard {shlex.quote(repository.base_branch)}; git clean -fdq"
    )
    (build_dir / "Dockerfile").write_text(
        "\n".join(docker_lines) + "\n",
        encoding="utf-8",
    )

    base_repository = repository.base_image
    if ":" in base_repository.rsplit("/", 1)[-1]:
        base_repository = base_repository.rsplit(":", 1)[0]
    run_slug = re.sub(r"[^a-z0-9_.-]+", "-", run_dir.name.lower()).strip("-.")
    profile_slug = _slug(repository.profile)
    image_name = (
        f"{base_repository}:{config.swesmith.local_image_tag_prefix}-"
        f"{run_slug}-{profile_slug}"
    )
    command: list[str | Path] = ["docker", "build", "-t", image_name, build_dir]
    run_command(
        command,
        cwd=config.workspace_root,
        group_wrapper=(
            config.sweagent.docker_group
            if config.sweagent.docker_group_wrapper
            else None
        ),
    )
    return image_name, command


def _distribute_budget(total: int, count: int) -> list[int]:
    base, remainder = divmod(total, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _new_repository_evidence(
    repository: LocalRepositoryConfig,
    bug_budget: int,
) -> dict[str, Any]:
    profile_slug = _slug(repository.profile)
    return {
        "profile": repository.profile,
        "base_image": repository.base_image,
        "base_branch": repository.base_branch,
        "task_repo": repository.task_repo,
        "planned_bug_attempts": bug_budget,
        "status": "scheduled",
        "run_key": profile_slug,
        "jobs": [],
        "validation_commands": [],
        "artifacts": [],
    }


def _fail_started_attempt_stages(repository_evidence: dict[str, Any]) -> None:
    for job in repository_evidence.get("jobs", []):
        if job.get("status") == "running":
            job["status"] = "failed"


def _repository_has_failures(repository_evidence: dict[str, Any]) -> bool:
    return any(
        str(job.get("status", "")).startswith("failed")
        for job in repository_evidence.get("jobs", [])
    ) or any(
        not artifact.get("validated", False)
        or not artifact.get("issue_generated", False)
        for artifact in repository_evidence.get("artifacts", [])
    )


def _write_production_evidence(path: Path, payload: dict[str, Any]) -> None:
    jobs = [job for repository in payload["repositories"] for job in repository.get("jobs", [])]
    artifacts = [
        artifact
        for repository in payload["repositories"]
        for artifact in repository.get("artifacts", [])
    ]
    payload["summary"] = {
        "planned": payload["planned_bug_attempts"],
        "jobs_started": sum(job.get("status") != "scheduled" for job in jobs),
        "generated": len(artifacts),
        "validated": sum(bool(item.get("validated")) for item in artifacts),
        "issue_generated": sum(bool(item.get("issue_generated")) for item in artifacts),
        "selected_for_agent": sum(bool(item.get("selected_for_agent")) for item in artifacts),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _round_robin_by_producer(
    repository_groups: list[list[dict[str, Any]]],
    producer_order: list[str],
) -> list[dict[str, Any]]:
    grouped = [
        {
            producer: [
                task
                for task in repository_tasks
                if task.get("_wm_generator_id") == producer
            ]
            for producer in producer_order
        }
        for repository_tasks in repository_groups
    ]
    positions = [
        {producer: 0 for producer in producer_order} for _ in repository_groups
    ]
    merged: list[dict[str, Any]] = []
    while True:
        added = False
        for producer in producer_order:
            for repository_index, repository in enumerate(grouped):
                position = positions[repository_index][producer]
                tasks = repository[producer]
                if position < len(tasks):
                    merged.append(tasks[position])
                    positions[repository_index][producer] += 1
                    added = True
        if not added:
            return merged


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")


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
