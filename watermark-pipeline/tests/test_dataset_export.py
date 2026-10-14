from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from watermark_pipeline.export.dataset import write_decisions
from watermark_pipeline.schemas import SamplingDecision


class DatasetExportTest(unittest.TestCase):
    def test_writes_expanded_json_arrays_instead_of_jsonl(self) -> None:
        decision = SamplingDecision(
            instance_id="example.instance",
            problem_statement="Fix the parser.",
            source_trajectory="trajectories/example.instance/example.instance.traj",
            original_terminal_response="Implemented the parser fix.",
            decision="accepted",
            reason="watermark_match",
        )

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            counts = write_decisions(
                run_dir,
                [decision],
                algorithm="semantic-sro-hmac-v2",
                key_id="test-key",
            )

            accepted_path = run_dir / "accepted.json"
            text = accepted_path.read_text(encoding="utf-8")
            records = json.loads(text)

            self.assertEqual(counts["accepted"], 1)
            self.assertEqual(records[0]["instance_id"], "example.instance")
            self.assertIn("\n  {\n", text)
            self.assertTrue(text.endswith("\n"))
            self.assertFalse((run_dir / "accepted.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
