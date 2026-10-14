from __future__ import annotations

import hashlib
import hmac

from watermark_pipeline.schemas import SRO


ALGORITHM = "canonical-response-sro-watermark-hmac-v4"

_CODE_DOMAIN = b"watermark-code-v1"
_TASK_DOMAIN = b"task-sro-index-v1"
_RESPONSE_DOMAIN = b"response-sro-verification-v1"


class WatermarkPredicate:
    def __init__(
        self,
        watermark: str,
        code_key: str,
        task_key: str,
        response_key: str,
        code_modulus: int,
        modulus: int,
        target: int,
    ):
        if not watermark or any(bit not in "01" for bit in watermark):
            raise RuntimeError("Watermark must be a non-empty binary string")
        if not 2 <= code_modulus <= 2**64:
            raise RuntimeError("Watermark code modulus must be between 2 and 2^64")
        if modulus < 2:
            raise RuntimeError("Watermark verification modulus must be at least 2")
        if not 0 <= target < modulus:
            raise RuntimeError("Watermark target must be within the verification modulus")
        self.watermark = watermark
        self.code_key = _decode_key("code", code_key)
        self.task_key = _decode_key("task-index", task_key)
        self.response_key = _decode_key("response-verification", response_key)
        self.code_modulus = code_modulus
        self.modulus = modulus
        self.target = target
        self._codes = tuple(
            self._hash_to_int(
                self.code_key,
                _CODE_DOMAIN,
                bit.encode("ascii"),
                index.to_bytes(8, byteorder="big", signed=False),
            )
            % self.code_modulus
            for index, bit in enumerate(self.watermark)
        )

    def task_index(self, task_sro: SRO) -> int:
        return self._hash_to_int(
            self.task_key,
            _TASK_DOMAIN,
            task_sro.canonical().encode("utf-8"),
        ) % len(self._codes)

    def selected_code(self, task_sro: SRO) -> int:
        return self._codes[self.task_index(task_sro)]

    def accepts(self, *, task_sro: SRO, response_sro: SRO) -> bool:
        code = self.selected_code(task_sro)
        value = self._hash_to_int(
            self.response_key,
            _RESPONSE_DOMAIN,
            response_sro.canonical().encode("utf-8"),
            code.to_bytes(8, byteorder="big", signed=False),
        )
        return value % self.modulus == self.target

    @staticmethod
    def _hash_to_int(key: bytes, domain: bytes, *parts: bytes) -> int:
        message = b"\x1e".join((domain, *parts))
        digest = hmac.new(key, message, hashlib.sha256).digest()
        return int.from_bytes(digest, byteorder="big", signed=False)


def _decode_key(name: str, value: str) -> bytes:
    try:
        key = bytes.fromhex(value)
    except ValueError as error:
        raise RuntimeError(f"Watermark {name} key must be hexadecimal") from error
    if len(key) != 32:
        raise RuntimeError(f"Watermark {name} key must encode exactly 32 bytes")
    return key
