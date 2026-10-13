from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


LOGGER = logging.getLogger(__name__)


def run_command(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    group_wrapper: str | None = None,
) -> None:
    argv = [str(item) for item in command]
    if group_wrapper:
        argv = ["sg", group_wrapper, "-c", shlex.join(argv)]
    LOGGER.info("Starting external command in %s: %s", cwd, shlex.join(argv))
    completed = subprocess.run(argv, cwd=cwd, env=dict(env) if env else None, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"External command failed with exit code {completed.returncode}: {shlex.join(argv)}")

