from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from watermark_pipeline.adapters.swesmith import (
    _local_combiner_command,
    _local_generator_command,
    _new_repository_evidence,
    _parent_instance_ids,
    _produce_local_repository_tasks,
)
from watermark_pipeline.config import (
    CombinerConfig,
    GeneratorConfig,
    PipelineConfig,
)


WORKSPACE = Path(__file__).resolve().parents[2]
BASE_CONFIG = WORKSPACE / "watermark-pipeline" / "configs" / "pipeline.yaml"


class SWESmithGenerationPlanTests(unittest.TestCase):
    def test_legacy_generator_is_converted_to_plan(self) -> None:
        config = PipelineConfig.load(BASE_CONFIG)

        self.assertEqual(len(config.swesmith.generators), 1)
        generator = config.swesmith.generators[0]
        self.assertEqual(generator.kind, config.swesmith.local_generator)
        self.assertEqual(generator.budget, config.swesmith.max_bugs)

    def test_parses_all_generators_and_combiners(self) -> None:
        raw = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
        raw["workspace_root"] = str(WORKSPACE)
        raw["swesmith"]["generators"] = [
            {
                "id": "modify",
                "kind": "lm_modify",
                "budget": 5,
                "config_file": "configs/bug_gen/lm_modify.yml",
                "model": "test/model",
            },
            {
                "id": "rewrite",
                "kind": "lm_rewrite",
                "budget": 5,
                "config_file": "configs/bug_gen/lm_rewrite.yml",
                "model": "test/model",
            },
            {"id": "procedural", "kind": "procedural", "budget": 5},
            {
                "id": "mirror",
                "kind": "mirror",
                "budget": 2,
                "input_files": ["inputs/prs.json"],
                "model": "test/model",
            },
        ]
        raw["swesmith"]["combiners"] = [
            {
                "id": "file",
                "kind": "combine_file",
                "sources": ["modify", "rewrite"],
                "num_patches": 2,
            },
            {
                "id": "module",
                "kind": "combine_module",
                "sources": ["procedural"],
                "num_patches": 2,
                "depth": 2,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            config = PipelineConfig.load(path)

        self.assertEqual(
            [generator.kind for generator in config.swesmith.generators],
            ["lm_modify", "lm_rewrite", "procedural", "mirror"],
        )
        self.assertEqual(
            [combiner.kind for combiner in config.swesmith.combiners],
            ["combine_file", "combine_module"],
        )

    def test_combiner_rejects_mirror_source(self) -> None:
        raw = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
        raw["workspace_root"] = str(WORKSPACE)
        raw["swesmith"]["generators"] = [
            {"id": "mirror", "kind": "mirror", "budget": 1, "input_files": ["prs.json"]}
        ]
        raw["swesmith"]["combiners"] = [
            {"kind": "combine_file", "sources": ["mirror"]}
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cannot consume mirror"):
                PipelineConfig.load(path)

    def test_rewrite_and_combiner_commands(self) -> None:
        config = PipelineConfig.load(BASE_CONFIG)
        repository = config.swesmith.local_repositories[0]
        generator = GeneratorConfig(
            id="rewrite",
            kind="lm_rewrite",
            budget=3,
            model="test/model",
            config_file="configs/bug_gen/lm_rewrite.yml",
            workers=2,
            input_files=(),
            max_files=8,
            max_lines=500,
            max_file_lines=10_000,
        )
        command = _local_generator_command(
            config,
            Path("/run/test"),
            Path("/run/test/tasks/repo"),
            repository,
            generator,
            3,
        )
        rendered = [str(part) for part in command]
        self.assertIn("swesmith.bug_gen.llm.rewrite", rendered)
        self.assertEqual(rendered[rendered.index("--max_bugs") + 1], "3")
        self.assertEqual(rendered[rendered.index("--n_workers") + 1], "2")

        combiner = CombinerConfig(
            id="combined",
            kind="combine_module",
            sources=("rewrite",),
            num_patches=2,
            limit=4,
            max_combos=20,
            depth=3,
        )
        combine_command = [
            str(part)
            for part in _local_combiner_command(
                repository, combiner, Path("/run/validated.json")
            )
        ]
        self.assertIn("swesmith.bug_gen.combine.same_module", combine_command)
        self.assertIn("--validated-instances-file", combine_command)
        self.assertEqual(combine_command[combine_command.index("--depth") + 1], "3")

    def test_mirror_command_writes_run_scoped_input_and_profile_map(self) -> None:
        config = PipelineConfig.load(BASE_CONFIG)
        repository = config.swesmith.local_repositories[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "prs.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "repo": "pallets/markupsafe",
                        "instance_id": "pallets__markupsafe-1",
                        "pull_number": 1,
                        "patch": "diff",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            generator = GeneratorConfig(
                id="mirror",
                kind="mirror",
                budget=1,
                model="test/model",
                config_file=None,
                workers=1,
                input_files=(source,),
                max_files=8,
                max_lines=500,
                max_file_lines=10_000,
            )
            (root / "repo").mkdir()
            command = _local_generator_command(
                config, root, root / "repo", repository, generator, 1
            )
            rendered = [str(part) for part in command]
            profile_map = Path(rendered[rendered.index("--profile-map") + 1])
            mirror_input = Path(rendered[3])

            self.assertEqual(
                json.loads(profile_map.read_text(encoding="utf-8")),
                {"pallets/markupsafe": repository.profile},
            )
            self.assertEqual(len(json.loads(mirror_input.read_text(encoding="utf-8"))), 1)

    def test_combination_parent_ids_are_normalized(self) -> None:
        parents = _parent_instance_ids(
            "owner__repo.12345678",
            {"patch_files": ["bug__lm_modify__abc.diff", "bug__func_pm_x__def"]},
        )
        self.assertEqual(
            parents,
            [
                "owner__repo.12345678.lm_modify__abc",
                "owner__repo.12345678.func_pm_x__def",
            ],
        )

    def test_direct_then_combine_dag(self) -> None:
        config = PipelineConfig.load(BASE_CONFIG)
        direct = replace(config.swesmith.generators[0], id="direct", budget=2)
        combiner = CombinerConfig(
            id="combined",
            kind="combine_file",
            sources=("direct",),
            num_patches=2,
            limit=1,
            max_combos=10,
            depth=3,
        )
        config = replace(
            config,
            swesmith=replace(config.swesmith, generators=(direct,), combiners=(combiner,)),
        )
        repository = config.swesmith.local_repositories[0]
        evidence = _new_repository_evidence(repository, 2)
        direct_id = f"{repository.profile}.lm_modify__direct"
        second_direct_id = f"{repository.profile}.lm_modify__second"
        combined_id = f"{repository.profile}.combine_file__combined"

        def candidates(*args):
            producer_ids = args[3]
            return [
                {
                    "instance_id": instance_id,
                    "repo": repository.task_repo,
                    "patch": "diff",
                    "_wm_generator_id": producer_id,
                }
                for instance_id, producer_id in producer_ids.items()
            ]

        def validated(_config, _repository, _directory, values, _label, _evidence):
            return [
                {
                    **value,
                    "FAIL_TO_PASS": ["tests/test_x.py::test_x"],
                    "PASS_TO_PASS": ["tests/test_y.py::test_y"],
                }
                for value in values
            ]

        def issues(_config, _directory, values, _env):
            return ([{**value, "problem_statement": "Fix it"} for value in values], [])

        with tempfile.TemporaryDirectory() as temporary, patch(
            "watermark_pipeline.adapters.swesmith._bug_artifact_ids",
            side_effect=[
                set(),
                {direct_id, second_direct_id},
                {direct_id, second_direct_id},
                {direct_id, second_direct_id, combined_id},
            ],
        ), patch(
            "watermark_pipeline.adapters.swesmith._local_generator_command",
            return_value=["generate"],
        ), patch(
            "watermark_pipeline.adapters.swesmith._local_combiner_command",
            return_value=["combine"],
        ), patch(
            "watermark_pipeline.adapters.swesmith._collect_candidates",
            side_effect=candidates,
        ), patch(
            "watermark_pipeline.adapters.swesmith._validate_candidates",
            side_effect=validated,
        ), patch(
            "watermark_pipeline.adapters.swesmith._generate_local_issues",
            side_effect=issues,
        ), patch(
            "watermark_pipeline.adapters.swesmith._build_local_task_image",
            return_value=("image:test", ["docker", "build"]),
        ), patch("watermark_pipeline.adapters.swesmith.run_command") as run:
            tasks = _produce_local_repository_tasks(
                config,
                Path(temporary),
                Path(temporary) / "tasks",
                repository,
                {"direct": 2},
                {},
                evidence,
                lambda: None,
            )

        self.assertEqual(
            [task["instance_id"] for task in tasks],
            sorted([direct_id, second_direct_id]) + [combined_id],
        )
        self.assertEqual([job["status"] for job in evidence["jobs"]], ["succeeded", "succeeded"])
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
