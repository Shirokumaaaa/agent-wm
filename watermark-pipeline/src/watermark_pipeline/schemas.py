from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class SRO:
    subject: str
    relation: str
    object: str

    def canonical(self) -> str:
        return "\x1f".join(
            _normalize(value) for value in (self.subject, self.relation, self.object)
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ResponseSRO:
    compressed_sentence: str
    subject: str
    relation: str
    object: str

    @classmethod
    def empty(cls, compressed_sentence: str = "") -> "ResponseSRO":
        return cls(
            compressed_sentence=compressed_sentence,
            subject="",
            relation="",
            object="",
        )

    def primary_sro(self) -> SRO:
        return SRO(subject=self.subject, relation=self.relation, object=self.object)

    def has_primary_sro(self) -> bool:
        return bool(self.relation)


@dataclass(frozen=True)
class TerminalResponse:
    response: str
    trajectory_path: Path
    trajectory_data: dict[str, Any]


@dataclass(frozen=True)
class SamplingDecision:
    instance_id: str
    problem_statement: str
    source_trajectory: str
    decision: Literal["accepted", "rejected", "ineligible", "error"]
    reason: str
    attempts: int = 0
    sampled_terminal_response: str | None = None
    sampled_compressed_sentence: str | None = None
    task_sro: SRO | None = None
    sampled_response_sro: SRO | None = None
    derived_trajectory: str | None = None

    def to_dict(self, *, algorithm: str, key_id: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "instance_id": self.instance_id,
            "problem_statement": self.problem_statement,
            "source_trajectory": self.source_trajectory,
            "sampled_terminal_response": self.sampled_terminal_response,
            "sampled_compressed_sentence": self.sampled_compressed_sentence,
            "task_sro": self.task_sro.to_dict() if self.task_sro else None,
            "sampled_response_sro": (
                self.sampled_response_sro.to_dict() if self.sampled_response_sro else None
            ),
            "sampling": {
                "decision": self.decision,
                "reason": self.reason,
                "attempts": self.attempts,
                "algorithm": algorithm,
                "key_id": key_id,
            },
        }
        if self.derived_trajectory:
            data["derived_trajectory"] = self.derived_trajectory
        return data


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
