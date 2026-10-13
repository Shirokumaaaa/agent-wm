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
    local_profile: str
    local_generator: str
    local_generator_config: str
    local_generator_model: str
    local_generation_workers: int
    local_validation_workers: int
    local_base_image: str
    local_base_branch: str
    local_task_repo: str
    local_issue_mode: str
    local_image_tag_prefix: str


@dataclass(frozen=True)
class SWEAgentConfig:
    executable: Path
    project_dir: Path
    config: Path
    docker_group_wrapper: bool
    docker_group: str
    container_proxy_url: str | None
    num_workers: int | None


@dataclass(frozen=True)
class SamplerConfig:
    model: str
    api_base: str
    api_key_env: str
    secret_key_env: str
    key_id: str
    max_attempts: int
    candidates_per_round: int
    modulus: int
    target: int
    allow_ephemeral_secret: bool


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

        existing_task = smith.get("existing_task_file")
        return cls(
            workspace_root=workspace_root,
            project_dir=project_dir,
            run=RunConfig(
                output_root=_resolve(workspace_root, run["output_root"]),
                resume=bool(run.get("resume", True)),
            ),
            swesmith=SWESmithConfig(
                task_source=str(smith.get("task_source", "modal" if smith.get("enabled", False) else "existing")),
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
                task_limit=(int(smith["task_limit"]) if smith.get("task_limit") is not None else None),
                required_task_count=(
                    int(smith["required_task_count"])
                    if smith.get("required_task_count") is not None
                    else None
                ),
                existing_task_file=(
                    _resolve(workspace_root, existing_task) if existing_task else None
                ),
                local_profile=str(smith.get("local_profile", "")),
                local_generator=str(smith.get("local_generator", "procedural")),
                local_generator_config=str(
                    smith.get("local_generator_config", "configs/bug_gen/lm_modify.yml")
                ),
                local_generator_model=str(
                    smith.get("local_generator_model", "deepseek/deepseek-flash")
                ),
                local_generation_workers=int(smith.get("local_generation_workers", 1)),
                local_validation_workers=int(smith.get("local_validation_workers", 4)),
                local_base_image=str(smith.get("local_base_image", "")),
                local_base_branch=str(smith.get("local_base_branch", "main")),
                local_task_repo=str(smith.get("local_task_repo", "")),
                local_issue_mode=str(smith.get("local_issue_mode", "static")),
                local_image_tag_prefix=str(
                    smith.get("local_image_tag_prefix", "wm-local")
                ),
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
                num_workers=(
                    int(agent["num_workers"]) if agent.get("num_workers") is not None else None
                ),
            ),
            sampler=SamplerConfig(
                model=str(sampler["model"]),
                api_base=str(sampler["api_base"]),
                api_key_env=str(sampler["api_key_env"]),
                secret_key_env=str(sampler["secret_key_env"]),
                key_id=str(sampler.get("key_id", "default")),
                max_attempts=int(sampler.get("max_attempts", 32)),
                candidates_per_round=int(sampler.get("candidates_per_round", 4)),
                modulus=int(sampler.get("modulus", 16)),
                target=int(sampler.get("target", 0)),
                allow_ephemeral_secret=bool(sampler.get("allow_ephemeral_secret", False)),
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
