from __future__ import annotations

import os
from typing import Any

import litellm

from watermark_pipeline.config import SamplerConfig


class DeepSeekClient:
    def __init__(self, config: SamplerConfig):
        self.config = config
        self.api_key = os.environ.get(config.api_key_env, "")
        if not self.api_key:
            raise RuntimeError(f"Missing required environment variable: {config.api_key_env}")

    def complete(self, system: str, user: str) -> str:
        response: Any = litellm.completion(
            model=self.config.model,
            api_base=self.config.api_base,
            api_key=self.api_key,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise RuntimeError("DeepSeek returned an empty or non-text response")
        return content

