from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class RunConfig:
    output_root: Path
    resume: bool


@dataclass(frozen=True)
class LocalRepositoryConfig:
    profile: str
    base_image: str
    base_branch: str
    task_repo: str


@dataclass(frozen=True)
class GeneratorConfig:
    id: str
    kind: str
    budget: int
    model: str | None
    config_file: str | None
    workers: int
    input_files: tuple[Path, ...]
    max_files: int
    max_lines: int
    max_file_lines: int


@dataclass(frozen=True)
class CombinerConfig:
    id: str
    kind: str
    sources: tuple[str, ...]
    num_patches: int
    limit: int
    max_combos: int
    depth: int


@dataclass(frozen=True)
class SWESmithConfig:
    task_source: str
    enabled: bool
    project_dir: Path
    modal_executable: Path
    script: Path
    repos: str
    language: str
    phases: str
    max_bugs: int
    issue_gen_config: str
    issue_gen_workers: int
    modal_volume: str
    task_remote_path: str
    task_file_glob: str
    task_limit: int | None
    required_task_count: int | None
    existing_task_file: Path | None
    local_repositories: tuple[LocalRepositoryConfig, ...]
    local_repository_limit: int
    local_repository_workers: int
    local_generation_workers: int
    local_validation_workers: int
    local_issue_mode: str
    mirror_issue_mode: str
    mirror_issue_fallback: str
    local_image_tag_prefix: str
    generators: tuple[GeneratorConfig, ...]
    combiners: tuple[CombinerConfig, ...]


@dataclass(frozen=True)
class SWEAgentConfig:
    executable: Path
    project_dir: Path
    config: Path
    docker_group_wrapper: bool
    docker_group: str
    container_proxy_url: str | None
    container_network_mode: str | None
    startup_timeout: float | None
    num_workers: int | None


@dataclass(frozen=True)
class SamplerConfig:
    model: str
    api_base: str
    api_key_env: str
    watermark: str
    code_key: str
    task_key: str
    response_key: str
    key_id: str
    max_attempts: int
    candidates_per_round: int
    code_modulus: int
    modulus: int
    target: int


@dataclass(frozen=True)
class ExportConfig:
    write_derived_trajectory: bool


@dataclass(frozen=True)
class PipelineConfig:
    workspace_root: Path
    run: RunConfig
    swesmith: SWESmithConfig
    sweagent: SWEAgentConfig
    sampler: SamplerConfig
    export: ExportConfig
    project_dir: Path

    @classmethod
    def load(cls, path: Path) -> "PipelineConfig":
        config_path = path.resolve()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        raw = _expand_environment(raw)
        config_dir = config_path.parent
        workspace_root = _resolve(config_dir, raw.get("workspace_root", "."))
        project_dir = config_dir.parent

        env_file = workspace_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)

        run = raw["run"]
        smith = raw["swesmith"]
        agent = raw["sweagent"]
        sampler = raw["sampler"]
        export = raw.get("export", {})
        local_repositories = _parse_local_repositories(smith)
        task_source = str(
            smith.get("task_source", "modal" if smith.get("enabled", False) else "existing")
        )
        generators = _parse_generators(smith, workspace_root, task_source=task_source)
        combiners = _parse_combiners(smith, generators)

        existing_task = smith.get("existing_task_file")
        return cls(
            workspace_root=workspace_root,
            project_dir=project_dir,
            run=RunConfig(
                output_root=_resolve(workspace_root, run["output_root"]),
                resume=bool(run.get("resume", True)),
            ),
            swesmith=SWESmithConfig(
                task_source=task_source,
                enabled=bool(smith.get("enabled", False)),
                project_dir=_resolve(workspace_root, smith["project_dir"]),
                modal_executable=_resolve(
                    workspace_root, smith.get("modal_executable", ".venv/bin/modal")
                ),
                script=Path(smith.get("script", "scripts/bug_gen_modal.py")),
                repos=str(smith.get("repos", "")),
                language=str(smith.get("language", "python")),
                phases=str(smith.get("phases", "gen,val,gather,issue")),
                max_bugs=int(smith.get("max_bugs", 200)),
                issue_gen_config=str(
                    smith.get("issue_gen_config", "configs/issue_gen/ig_v2.yaml")
                ),
                issue_gen_workers=int(smith.get("issue_gen_workers", 8)),
                modal_volume=str(smith.get("modal_volume", "swesmith-bug-gen")),
                task_remote_path=str(smith.get("task_remote_path", "python/task_insts")),
                task_file_glob=str(smith.get("task_file_glob", "**/*__ig_llm.json")),
                task_limit=(
                    int(smith["task_limit"])
                    if smith.get("task_limit") is not None
                    else None
                ),
                required_task_count=(
                    int(smith["required_task_count"])
                    if smith.get("required_task_count") is not None
                    else None
                ),
                existing_task_file=(
                    _resolve(workspace_root, existing_task) if existing_task else None
                ),
                local_repositories=local_repositories,
                local_repository_limit=int(
                    smith.get("local_repository_limit", len(local_repositories))
                ),
                local_repository_workers=int(
                    smith.get("local_repository_workers", 1)
                ),
                local_generation_workers=int(smith.get("local_generation_workers", 1)),
                local_validation_workers=int(smith.get("local_validation_workers", 4)),
                local_issue_mode=str(smith.get("local_issue_mode", "static")),
                mirror_issue_mode=str(
                    smith.get("mirror_issue_mode", "original_pr")
                ),
                mirror_issue_fallback=str(
                    smith.get("mirror_issue_fallback", smith.get("local_issue_mode", "static"))
                ),
                local_image_tag_prefix=str(
                    smith.get("local_image_tag_prefix", "wm-local")
                ),
                generators=generators,
                combiners=combiners,
            ),
            sweagent=SWEAgentConfig(
                executable=_resolve(workspace_root, agent["executable"]),
                project_dir=_resolve(workspace_root, agent["project_dir"]),
                config=_resolve(workspace_root, agent["config"]),
                docker_group_wrapper=bool(agent.get("docker_group_wrapper", False)),
                docker_group=str(agent.get("docker_group", "docker")),
                container_proxy_url=(
                    str(agent["container_proxy_url"]) if agent.get("container_proxy_url") else None
                ),
                container_network_mode=(
                    str(agent["container_network_mode"])
                    if agent.get("container_network_mode")
                    else None
                ),
                startup_timeout=(
                    float(agent["startup_timeout"])
                    if agent.get("startup_timeout") is not None
                    else None
                ),
                num_workers=(
                    int(agent["num_workers"]) if agent.get("num_workers") is not None else None
                ),
            ),
            sampler=SamplerConfig(
                model=str(sampler["model"]),
                api_base=str(sampler["api_base"]),
                api_key_env=str(sampler["api_key_env"]),
                watermark=str(sampler["watermark"]),
                code_key=str(sampler["code_key"]),
                task_key=str(sampler["task_key"]),
                response_key=str(sampler["response_key"]),
                key_id=str(sampler.get("key_id", "default")),
                max_attempts=int(sampler.get("max_attempts", 32)),
                candidates_per_round=int(sampler.get("candidates_per_round", 4)),
                code_modulus=int(sampler.get("code_modulus", 2**31 - 1)),
                modulus=int(sampler.get("modulus", 16)),
                target=int(sampler.get("target", 0)),
            ),
            export=ExportConfig(
                write_derived_trajectory=bool(export.get("write_derived_trajectory", True))
            ),
        )


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, str):
        return os.path.expandvars(value)
    return value


def _parse_local_repositories(smith: dict[str, Any]) -> tuple[LocalRepositoryConfig, ...]:
    raw_repositories = smith.get("local_repositories")
    if raw_repositories is None:
        profile = str(smith.get("local_profile", ""))
        if not profile:
            return ()
        raw_repositories = [
            {
                "profile": profile,
                "base_image": smith.get("local_base_image", ""),
                "base_branch": smith.get("local_base_branch", "main"),
                "task_repo": smith.get("local_task_repo", ""),
            }
        ]
    if not isinstance(raw_repositories, list):
        raise RuntimeError("swesmith.local_repositories must be a list")

    repositories: list[LocalRepositoryConfig] = []
    for index, raw_repository in enumerate(raw_repositories):
        if isinstance(raw_repository, str):
            values: dict[str, Any] = {"profile": raw_repository}
        elif isinstance(raw_repository, dict):
            values = raw_repository
        else:
            raise RuntimeError(
                f"swesmith.local_repositories[{index}] must be a string or mapping"
            )
        profile = str(values.get("profile", "")).strip()
        if not profile:
            raise RuntimeError(
                f"swesmith.local_repositories[{index}] has no profile"
            )
        default_image, default_task_repo = _local_repository_defaults(profile)
        base_branch = str(values.get("base_branch", "main")).strip()
        if not base_branch:
            raise RuntimeError(
                f"swesmith.local_repositories[{index}] has no base_branch"
            )
        repositories.append(
            LocalRepositoryConfig(
                profile=profile,
                base_image=str(values.get("base_image") or default_image),
                base_branch=base_branch,
                task_repo=str(values.get("task_repo") or default_task_repo),
            )
        )
    profiles = [repository.profile for repository in repositories]
    if len(set(profiles)) != len(profiles):
        raise RuntimeError("swesmith.local_repositories contains duplicate profiles")
    return tuple(repositories)


def _parse_generators(
    smith: dict[str, Any],
    workspace_root: Path,
    *,
    task_source: str,
) -> tuple[GeneratorConfig, ...]:
    raw_generators = smith.get("generators")
    if not raw_generators:
        # Only the bug-producing sources need generators; replay sources reuse
        # existing task files and never invoke SWE-smith bug generation.
        if task_source in {"local_production", "production", "modal"}:
            raise RuntimeError(
                f"swesmith.generators must be a non-empty list of generator "
                f"mappings when task_source is {task_source!r}"
            )
        return ()
    if not isinstance(raw_generators, list):
        raise RuntimeError("swesmith.generators must be a list")

    supported = {"lm_modify", "lm_rewrite", "procedural", "mirror"}
    generators: list[GeneratorConfig] = []
    for index, raw in enumerate(raw_generators):
        if not isinstance(raw, dict):
            raise RuntimeError(f"swesmith.generators[{index}] must be a mapping")
        kind = str(raw.get("kind", "")).strip()
        generator_id = str(raw.get("id", kind)).strip()
        if kind not in supported:
            raise RuntimeError(
                f"Unsupported SWE-smith generator {kind!r}; expected one of {sorted(supported)}"
            )
        if not generator_id:
            raise RuntimeError(f"swesmith.generators[{index}] has no id")
        budget = int(raw.get("budget", smith.get("max_bugs", 200)))
        if budget <= 0:
            raise RuntimeError(f"Generator {generator_id!r} budget must be positive")
        raw_input_files = raw.get("input_files", [])
        if not isinstance(raw_input_files, list):
            raise RuntimeError(f"Generator {generator_id!r} input_files must be a list")
        input_files = tuple(
            _resolve(workspace_root, value) for value in raw_input_files
        )
        if kind == "mirror" and not input_files:
            raise RuntimeError(f"Mirror generator {generator_id!r} requires input_files")
        config_file = raw.get("config_file")
        if config_file is None and kind in {"lm_modify", "lm_rewrite"}:
            config_file = f"configs/bug_gen/{kind}.yml"
        if kind in {"lm_modify", "lm_rewrite"} and not config_file:
            raise RuntimeError(f"Generator {generator_id!r} requires config_file")
        workers = int(raw.get("workers", smith.get("local_generation_workers", 1)))
        if workers <= 0:
            raise RuntimeError(f"Generator {generator_id!r} workers must be positive")
        model = raw.get("model")
        if model is None and kind in {"lm_modify", "lm_rewrite", "mirror"}:
            model = "openai/gpt-4o"
        generators.append(
            GeneratorConfig(
                id=generator_id,
                kind=kind,
                budget=budget,
                model=str(model) if model else None,
                config_file=str(config_file) if config_file else None,
                workers=workers,
                input_files=input_files,
                max_files=int(raw.get("max_files", 8)),
                max_lines=int(raw.get("max_lines", 500)),
                max_file_lines=int(raw.get("max_file_lines", 10_000)),
            )
        )
    ids = [generator.id for generator in generators]
    if len(ids) != len(set(ids)):
        raise RuntimeError("swesmith.generators contains duplicate ids")
    return tuple(generators)


def _parse_combiners(
    smith: dict[str, Any], generators: tuple[GeneratorConfig, ...]
) -> tuple[CombinerConfig, ...]:
    raw_combiners = smith.get("combiners", [])
    if not isinstance(raw_combiners, list):
        raise RuntimeError("swesmith.combiners must be a list")
    source_ids = {generator.id for generator in generators}
    combiners: list[CombinerConfig] = []
    for index, raw in enumerate(raw_combiners):
        if not isinstance(raw, dict):
            raise RuntimeError(f"swesmith.combiners[{index}] must be a mapping")
        kind = str(raw.get("kind", "")).strip()
        combiner_id = str(raw.get("id", kind)).strip()
        if kind not in {"combine_file", "combine_module"}:
            raise RuntimeError(
                f"Unsupported SWE-smith combiner {kind!r}; expected combine_file or combine_module"
            )
        sources = tuple(str(value) for value in raw.get("sources", ()))
        if not sources:
            raise RuntimeError(f"Combiner {combiner_id!r} requires sources")
        unknown = set(sources) - source_ids
        if unknown:
            raise RuntimeError(
                f"Combiner {combiner_id!r} references unknown sources: {sorted(unknown)}"
            )
        kinds_by_id = {generator.id: generator.kind for generator in generators}
        mirror_sources = [source for source in sources if kinds_by_id[source] == "mirror"]
        if mirror_sources:
            raise RuntimeError(
                f"Combiner {combiner_id!r} cannot consume mirror sources: {mirror_sources}"
            )
        num_patches = int(raw.get("num_patches", 2))
        if num_patches < 2:
            raise RuntimeError(f"Combiner {combiner_id!r} num_patches must be at least 2")
        combiners.append(
            CombinerConfig(
                id=combiner_id,
                kind=kind,
                sources=sources,
                num_patches=num_patches,
                limit=int(raw.get("limit", -1)),
                max_combos=int(raw.get("max_combos", 100)),
                depth=int(raw.get("depth", 3)),
            )
        )
    ids = [generator.id for generator in generators] + [item.id for item in combiners]
    if len(ids) != len(set(ids)):
        raise RuntimeError("SWE-smith generator and combiner ids must be unique")
    return tuple(combiners)


def _local_repository_defaults(profile: str) -> tuple[str, str]:
    try:
        owner, repository_revision = profile.split("__", 1)
        repository, revision = repository_revision.rsplit(".", 1)
    except ValueError as error:
        raise RuntimeError(
            "Local SWE-smith profile must use owner__repository.revision format: "
            f"{profile!r}"
        ) from error
    if (
        not owner
        or not repository
        or len(revision) != 8
        or any(character not in "0123456789abcdefABCDEF" for character in revision)
    ):
        raise RuntimeError(f"Invalid local SWE-smith profile: {profile!r}")
    image = (
        "swebench/swesmith.x86_64."
        f"{owner}_1776_{repository}.{revision}"
    ).lower()
    return image, f"swesmith/{profile}"
