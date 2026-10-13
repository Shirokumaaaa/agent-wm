from __future__ import annotations

from watermark_pipeline.config import SamplerConfig
from watermark_pipeline.schemas import SRO
from watermark_pipeline.watermark.candidate import CandidateGenerator
from watermark_pipeline.watermark.predicate import WatermarkPredicate
from watermark_pipeline.watermark.sro import SROExtractor


class RejectionSampler:
    def __init__(
        self,
        config: SamplerConfig,
        extractor: SROExtractor,
        generator: CandidateGenerator,
        predicate: WatermarkPredicate,
    ):
        self.config = config
        self.extractor = extractor
        self.generator = generator
        self.predicate = predicate

    def sample(
        self,
        *,
        instance_id: str,
        problem_statement: str,
        original_response: str,
    ) -> tuple[str | None, int, SRO, SRO, SRO | None]:
        task_sro = self.extractor.from_task(problem_statement)
        original_sro = self.extractor.from_response(original_response)
        attempts = 0
        round_index = 0

        while attempts < self.config.max_attempts:
            remaining = self.config.max_attempts - attempts
            count = min(self.config.candidates_per_round, remaining)
            candidates = self.generator.generate(
                problem_statement=problem_statement,
                original_response=original_response,
                task_sro=task_sro,
                response_sro=original_sro,
                round_index=round_index,
                candidate_count=count,
            )
            for candidate in candidates[:count]:
                attempts += 1
                if self.predicate.accepts(
                    instance_id=instance_id,
                    task_sro=task_sro,
                    response=candidate,
                ):
                    sampled_sro = self.extractor.from_response(candidate)
                    return candidate, attempts, task_sro, original_sro, sampled_sro
            round_index += 1
            if not candidates:
                attempts += count

        return None, attempts, task_sro, original_sro, None

