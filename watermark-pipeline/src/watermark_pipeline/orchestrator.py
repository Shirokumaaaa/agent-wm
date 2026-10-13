from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from watermark_pipeline.adapters.sweagent import run_batch, validate_batch_outputs
from watermark_pipeline.adapters.swesmith import produce_tasks
from watermark_pipeline.config import PipelineConfig
from watermark_pipeline.export.dataset import write_decisions
from watermark_pipeline.llm.deepseek import DeepSeekClient
from watermark_pipeline.llm.prompts import PromptStore
from watermark_pipeline.schemas import SamplingDecision
from watermark_pipeline.state import RunState
from watermark_pipeline.trajectory.reader import find_trajectory, load_tasks
from watermark_pipeline.trajectory.terminal_response import (
    IneligibleTrajectory,
    read_terminal_response,
    write_derived_trajectory,
)
from watermark_pipeline.watermark.candidate import CandidateGenerator
from watermark_pipeline.watermark.predicate import ALGORITHM, WatermarkPredicate
from watermark_pipeline.watermark.sampler import RejectionSampler
from watermark_pipeline.watermark.sro import SROExtractor


LOGGER = logging.getLogger(__name__)


class PipelineRunner:
    def __init__(self, config: PipelineConfig, run_id: str | None = None):
        self.config = config
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = config.run.output_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state = RunState(self.run_dir / "manifest.json", self.run_id)

    def run(self) -> Path:
        task_file = self._task_stage()
        trajectory_root = self._agent_stage(task_file)
        decisions = self._sampling_stage(task_file, trajectory_root)
        self._export_stage(decisions)
        return self.run_dir

    def _task_stage(self) -> Path:
        stage = "task_production"
        if self.config.run.resume and self.state.stage_succeeded(stage):
            return Path(self.state.output(stage, "task_file"))
        self._invalidate_stages(
            ("agent_execution", "watermark_sampling", "dataset_export"),
            "task production is being rerun",
        )
        self.state.start(stage)
        try:
            result = produce_tasks(self.config, self.run_dir)
        except Exception as error:
            self.state.fail(stage, str(error))
            raise
        outputs = {"task_file": str(result.task_file)}
        if result.evidence_file is not None:
            outputs["production_evidence"] = str(result.evidence_file)
        self.state.succeed(stage, outputs)
        return result.task_file

    def _agent_stage(self, task_file: Path) -> Path:
        stage = "agent_execution"
        if self.config.run.resume and self.state.stage_succeeded(stage):
            trajectory_root = Path(self.state.output(stage, "trajectory_root"))
            try:
                validate_batch_outputs(task_file, trajectory_root)
            except Exception as error:
                LOGGER.warning(
                    "Cached SWE-agent stage is invalid and will be rerun: %s",
                    error,
                )
            else:
                return trajectory_root
        self._invalidate_stages(
            ("watermark_sampling", "dataset_export"),
            "SWE-agent execution is being rerun",
        )
        self.state.start(stage)
        try:
            trajectory_root = run_batch(self.config, task_file, self.run_dir)
        except Exception as error:
            self.state.fail(stage, str(error))
            raise
        self.state.succeed(
            stage,
            {
                "trajectory_root": str(trajectory_root),
                "validation_report": str(trajectory_root / "batch_validation.json"),
            },
        )
        return trajectory_root

    def _sampling_stage(self, task_file: Path, trajectory_root: Path) -> list[SamplingDecision]:
        stage = "watermark_sampling"
        if self.config.run.resume and self.state.stage_succeeded(stage):
            records = load_tasks(Path(self.state.output(stage, "decision_file")))
            return [_decision_from_dict(item) for item in records]

        self._invalidate_stages(
            ("dataset_export",),
            "watermark sampling is being rerun",
        )
        self.state.start(stage)
        decision_file = self.run_dir / "samples" / "decisions.json"
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decisions: list[SamplingDecision] = []
        try:
            sampler = self._make_sampler()
            for task in load_tasks(task_file):
                decisions.append(self._sample_task(task, trajectory_root, sampler))
            decision_file.write_text(
                __import__("json").dumps(
                    [decision.to_dict(algorithm=ALGORITHM, key_id=self.config.sampler.key_id) for decision in decisions],
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as error:
            self.state.fail(stage, str(error))
            raise
        self.state.succeed(stage, {"decision_file": str(decision_file)})
        return decisions

    def _sample_task(
        self,
        task: dict,
        trajectory_root: Path,
        sampler: RejectionSampler,
    ) -> SamplingDecision:
        instance_id = str(task["instance_id"])
        problem_statement = str(task.get("problem_statement", ""))
        trajectory_path = find_trajectory(trajectory_root, instance_id)
        if trajectory_path is None:
            return SamplingDecision(
                instance_id=instance_id,
                problem_statement=problem_statement,
                source_trajectory="",
                original_terminal_response="",
                decision="ineligible",
                reason="trajectory_not_found",
            )

        source = str(trajectory_path.relative_to(self.run_dir))
        try:
            terminal = read_terminal_response(trajectory_path)
        except IneligibleTrajectory as error:
            return SamplingDecision(
                instance_id=instance_id,
                problem_statement=problem_statement,
                source_trajectory=source,
                original_terminal_response="",
                decision="ineligible",
                reason=error.reason,
            )

        try:
            candidate, attempts, task_sro, original_sro, sampled_sro = sampler.sample(
                instance_id=instance_id,
                problem_statement=problem_statement,
                original_response=terminal.response,
            )
        except Exception as error:
            LOGGER.exception("Sampling failed for %s", instance_id)
            return SamplingDecision(
                instance_id=instance_id,
                problem_statement=problem_statement,
                source_trajectory=source,
                original_terminal_response=terminal.response,
                decision="error",
                reason=type(error).__name__,
            )

        if candidate is None:
            return SamplingDecision(
                instance_id=instance_id,
                problem_statement=problem_statement,
                source_trajectory=source,
                original_terminal_response=terminal.response,
                decision="rejected",
                reason="max_attempts",
                attempts=attempts,
                task_sro=task_sro,
                original_response_sro=original_sro,
            )

        derived_path: str | None = None
        if self.config.export.write_derived_trajectory:
            destination = self.run_dir / "derived_trajectories" / instance_id / f"{instance_id}.traj"
            write_derived_trajectory(terminal, candidate, destination)
            derived_path = str(destination.relative_to(self.run_dir))

        return SamplingDecision(
            instance_id=instance_id,
            problem_statement=problem_statement,
            source_trajectory=source,
            original_terminal_response=terminal.response,
            sampled_terminal_response=candidate,
            decision="accepted",
            reason="watermark_match",
            attempts=attempts,
            task_sro=task_sro,
            original_response_sro=original_sro,
            sampled_response_sro=sampled_sro,
            derived_trajectory=derived_path,
        )

    def _make_sampler(self) -> RejectionSampler:
        client = DeepSeekClient(self.config.sampler)
        prompts = PromptStore(self.config.project_dir / "prompts")
        extractor = SROExtractor(client, prompts)
        generator = CandidateGenerator(client, prompts)
        secret = os.environ.get(self.config.sampler.secret_key_env, "")
        if not secret and self.config.sampler.allow_ephemeral_secret:
            LOGGER.warning("Using an in-memory ephemeral watermark key for this demonstration run")
            secret = secrets.token_urlsafe(32)
        predicate = WatermarkPredicate(
            secret=secret,
            modulus=self.config.sampler.modulus,
            target=self.config.sampler.target,
        )
        return RejectionSampler(self.config.sampler, extractor, generator, predicate)

    def _export_stage(self, decisions: list[SamplingDecision]) -> None:
        stage = "dataset_export"
        if self.config.run.resume and self.state.stage_succeeded(stage):
            return
        self.state.start(stage)
        try:
            counts = write_decisions(
                self.run_dir,
                decisions,
                algorithm=ALGORITHM,
                key_id=self.config.sampler.key_id,
            )
        except Exception as error:
            self.state.fail(stage, str(error))
            raise
        self.state.succeed(stage, {"counts": counts})

    def _invalidate_stages(self, stages: tuple[str, ...], reason: str) -> None:
        for stage in stages:
            self.state.invalidate(stage, reason)


def _decision_from_dict(data: dict) -> SamplingDecision:
    from watermark_pipeline.schemas import SRO

    def sro(name: str) -> SRO | None:
        value = data.get(name)
        return SRO(**value) if value else None

    sampling = data["sampling"]
    return SamplingDecision(
        instance_id=data["instance_id"],
        problem_statement=data["problem_statement"],
        source_trajectory=data["source_trajectory"],
        original_terminal_response=data["original_terminal_response"],
        sampled_terminal_response=data.get("sampled_terminal_response"),
        decision=sampling["decision"],
        reason=sampling["reason"],
        attempts=int(sampling.get("attempts", 0)),
        task_sro=sro("task_sro"),
        original_response_sro=sro("original_response_sro"),
        sampled_response_sro=sro("sampled_response_sro"),
        derived_trajectory=data.get("derived_trajectory"),
    )
