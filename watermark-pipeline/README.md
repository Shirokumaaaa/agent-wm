# Terminal Response Watermark Pipeline

This is an external orchestration project. It does not import or modify the
internals of SWE-smith or SWE-agent. The two projects are invoked through their
existing command-line interfaces.

The production pipeline runs these stages:

1. Snapshot the existing SWE-smith task instance IDs in the configured Modal volume.
2. Invoke SWE-smith's complete `gen,val,gather,issue` production workflow.
3. Download the resulting `*__ig_llm.json` artifacts and select only instance IDs
   that did not exist in the pre-production snapshot. The run fails instead of
   falling back to historical tasks when no new instance was produced.
   `required_task_count` can make the run fail unless the requested number of
   genuinely new tasks is available; the production configuration requires 5.
4. Invoke `sweagent run-batch` with runtime path overrides.
5. Require `run_batch_exit_statuses.yaml` to cover every input instance, reject
   uncaught batch failures, and require one parseable, identity-matching `.traj`
   per instance. A cached successful stage is revalidated before resume.
6. Read the final natural-language response associated with `submit`.
7. Use DeepSeek to extract SROs and generate rewrite candidates.
8. Accept the first candidate satisfying the keyed watermark predicate, or
   reject the instance when the attempt budget is exhausted.
9. Export accepted, rejected, and ineligible records.

`task_source: production` is the normal mode. The `local_static` and `existing`
modes are retained only for explicitly requested replay/debug runs; they do not
constitute new task production.

The pipeline does not alter model sampling settings. It sends no temperature,
top-p, token-limit, or reasoning overrides to the sampler model, and it does
not rewrite the existing SWE-agent configuration.

## Entry point

Run from the repository root with the existing virtual environment:

```bash
.venv/bin/python watermark-pipeline/run.py \
  --config watermark-pipeline/configs/pipeline.yaml
```

The launcher adds the project's `src` directory to its own import path, so the
external package does not need to be installed into the repository virtual
environment.

All generated files are written below `watermark-pipeline/runs/` by default.
Original trajectory files are read-only inputs. Derived trajectories, when
enabled, are written under the external run directory.

For production runs, `tasks/task_production.json` records the SWE-smith
entrypoint, requested phases, pre/post task counts, newly observed instance IDs,
and the exact new instances passed to SWE-agent. It does not contain trajectory
or patch hashes.

After SWE-agent returns, `trajectories/batch_validation.json` records the
per-instance batch status and trajectory path. `agent_execution` is marked
successful only after this report succeeds; the report path is also stored in
the run manifest.

## Required environment variables

- `DEEPSEEK_API_KEY`: DeepSeek API credential.
- `WM_SECRET_KEY`: secret used by the watermark predicate.
- Any credentials already required by the selected SWE-smith and SWE-agent
  configurations, including Modal authentication and the Modal secrets used by
  SWE-smith (`GITHUB_TOKEN` and `PORTKEY_API_KEY` for the current production
  script).

Secrets are never included in run manifests or exported dataset records.
