from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from watermark_pipeline.adapters.sweagent import _write_runtime_config


def test_runtime_config_uses_host_network_for_loopback_proxy(tmp_path: Path) -> None:
    source = tmp_path / "sweagent.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "instances": {
                    "deployment": {
                        "docker_args": ["--memory=1g", "--network=bridge"]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    task_file = tmp_path / "tasks.json"
    task_file.write_text("[]", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = SimpleNamespace(
        sweagent=SimpleNamespace(
            config=source,
            num_workers=5,
            startup_timeout=600.0,
            container_proxy_url="http://127.0.0.1:17894",
            container_network_mode="host",
        )
    )

    runtime_path = _write_runtime_config(config, task_file, tmp_path / "out", run_dir)
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    docker_args = runtime["instances"]["deployment"]["docker_args"]

    assert "--network=host" in docker_args
    assert "--network=bridge" not in docker_args
    assert "--add-host=host.docker.internal:host-gateway" not in docker_args
    assert "--env=HTTP_PROXY=http://127.0.0.1:17894" in docker_args
