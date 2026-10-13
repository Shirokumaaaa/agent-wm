from __future__ import annotations

import hashlib
import hmac

from watermark_pipeline.schemas import SRO


ALGORITHM = "terminal-response-sro-hmac-v1"


class WatermarkPredicate:
    def __init__(self, secret: str, modulus: int, target: int):
        if not secret:
            raise RuntimeError("Watermark secret is empty")
        self.secret = secret.encode("utf-8")
        self.modulus = modulus
        self.target = target

    def accepts(self, *, instance_id: str, task_sro: SRO, response: str) -> bool:
        message = "\x1e".join(
            (
                ALGORITHM,
                instance_id,
                task_sro.canonical(),
                _canonicalize_response(response),
            )
        ).encode("utf-8")
        digest = hmac.new(self.secret, message, hashlib.sha256).digest()
        value = int.from_bytes(digest[:8], byteorder="big", signed=False)
        return value % self.modulus == self.target


def _canonicalize_response(response: str) -> str:
    return "\n".join(line.rstrip() for line in response.strip().replace("\r\n", "\n").split("\n"))

