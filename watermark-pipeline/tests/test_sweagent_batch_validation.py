from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from watermark_pipeline.adapters.sweagent import validate_batch_outputs
from watermark_pipeline.orchestrator import PipelineRunner
from watermark_pipeline.state import RunState


class BatchValidationTests(unittest.TestCase):
    def test_rejects_zero_trajectory_uncaught_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_file = self._write_tasks(root, ["example.instance"])
            output_dir = root / "trajectories"
            output_dir.mkdir()
            self._write_statuses(
                output_dir,
                {"Uncaught RuntimeError": ["example.instance"]},
            )

            with self.assertRaisesRegex(RuntimeError, "uncaught exceptions"):
                validate_batch_outputs(task_file, output_dir)

            report = json.loads((output_dir / "batch_validation.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("trajectory not found" in error for error in report["errors"]))

    def test_accepts_complete_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_file = self._write_tasks(root, ["example.instance"])
            output_dir = root / "trajectories"
            self._write_valid_trajectory(output_dir, "example.instance", "submitted")
            self._write_statuses(output_dir, {"submitted": ["example.instance"]})

            report = validate_batch_outputs(task_file, output_dir)

            self.assertEqual(report["status"], "succeeded")
            self.assertEqual(
                report["trajectory_files"],
                {"example.instance": "example.instance/example.instance.traj"},
            )

    def test_invalid_cached_agent_stage_is_rerun_and_invalidates_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_file = self._write_tasks(root, ["example.instance"])
            output_dir = root / "trajectories"
            output_dir.mkdir()
            self._write_statuses(
                output_dir,
                {"Uncaught RuntimeError": ["example.instance"]},
            )

            state = RunState(root / "manifest.json", "test-run")
            state.start("agent_execution")
            state.succeed("agent_execution", {"trajectory_root": str(output_dir)})
            state.start("watermark_sampling")
            state.succeed("watermark_sampling", {"decision_file": str(root / "old.json")})
            state.start("dataset_export")
            state.succeed("dataset_export", {"counts": {"ineligible": 1}})

            runner = object.__new__(PipelineRunner)
            runner.config = SimpleNamespace(run=SimpleNamespace(resume=True))
            runner.run_dir = root
            runner.state = state

            def replacement_run_batch(config, supplied_task_file, supplied_run_dir):
                self.assertEqual(supplied_task_file, task_file)
                self.assertEqual(supplied_run_dir, root)
                self._write_valid_trajectory(output_dir, "example.instance", "submitted")
                self._write_statuses(output_dir, {"submitted": ["example.instance"]})
                validate_batch_outputs(task_file, output_dir)
                return output_dir

            with patch("watermark_pipeline.orchestrator.run_batch", replacement_run_batch):
                result = runner._agent_stage(task_file)

            self.assertEqual(result, output_dir)
            self.assertEqual(state.data["stages"]["agent_execution"]["status"], "succeeded")
            self.assertEqual(state.data["stages"]["watermark_sampling"]["status"], "invalidated")
            self.assertEqual(state.data["stages"]["dataset_export"]["status"], "invalidated")

    @staticmethod
    def _write_tasks(root: Path, instance_ids: list[str]) -> Path:
        path = root / "tasks.json"
        path.write_text(
            json.dumps([{"instance_id": instance_id} for instance_id in instance_ids]),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _write_statuses(output_dir: Path, grouped: dict[str, list[str]]) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run_batch_exit_statuses.yaml").write_text(
            yaml.safe_dump({"instances_by_exit_status": grouped, "total_cost": 0}),
            encoding="utf-8",
        )

    @staticmethod
    def _write_valid_trajectory(output_dir: Path, instance_id: str, status: str) -> None:
        path = output_dir / instance_id / f"{instance_id}.traj"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "environment": instance_id,
                    "info": {"exit_status": status},
                    "trajectory": [],
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
