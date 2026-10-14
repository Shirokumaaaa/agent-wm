from __future__ import annotations

import hashlib
import hmac
import unittest

from watermark_pipeline.schemas import SRO
from watermark_pipeline.watermark.predicate import WatermarkPredicate


class WatermarkPredicateTest(unittest.TestCase):
    def test_uses_only_canonical_task_and_response_sros(self) -> None:
        task_sro = SRO(" Parser ", "FIXES", "  escaped   delimiters ")
        response_sro = SRO("Decoder", "Preserves", "Literal values")
        secret = "test-secret"
        modulus = 17

        message = "\x1e".join(
            (task_sro.canonical(), response_sro.canonical())
        ).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
        expected_target = int.from_bytes(digest[:8], byteorder="big", signed=False) % modulus

        predicate = WatermarkPredicate(
            secret=secret,
            modulus=modulus,
            target=expected_target,
        )

        self.assertTrue(
            predicate.accepts(task_sro=task_sro, response_sro=response_sro)
        )

    def test_surface_formatting_is_removed_by_sro_canonicalization(self) -> None:
        predicate = WatermarkPredicate(secret="test-secret", modulus=16, target=0)
        task_a = SRO("Parser", "fixes", "escaped delimiters")
        task_b = SRO("  PARSER ", " FIXES", "escaped   delimiters ")
        response_a = SRO("Decoder", "preserves", "literal values")
        response_b = SRO(" DECODER", "PRESERVES ", "literal   values")

        self.assertEqual(
            predicate.accepts(task_sro=task_a, response_sro=response_a),
            predicate.accepts(task_sro=task_b, response_sro=response_b),
        )


if __name__ == "__main__":
    unittest.main()
