from __future__ import annotations

from pathlib import Path


class PromptStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def render(self, name: str, **values: object) -> str:
        text = (self.directory / name).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text

