# Terminal Response Watermark Pipeline

This is an external orchestration project. It does not import or modify the
internals of SWE-smith or SWE-agent. The two projects are invoked through their
existing command-line interfaces.

The production pipeline runs these stages:

1. Snapshot the existing local SWE-smith bug artifact IDs.
2. Invoke SWE-smith's local bug generator and collect only artifacts created by
   the current run.
3. Invoke SWE-smith's local Docker validation harness, select only candidates
   with non-empty `FAIL_TO_PASS` and `PASS_TO_PASS` sets, and build a run-scoped
   local Docker image containing their bug branches. No branch is pushed to a
   Git remote.
4. Invoke SWE-smith's local issue generator and require the configured number
   of complete, newly produced task instances. The production configuration
   requires 5.
5. Invoke `sweagent run-batch` with runtime path overrides.
6. Require `run_batch_exit_statuses.yaml` to cover every input instance, reject
   uncaught batch failures, and require one parseable, identity-matching `.traj`
   per instance. A cached successful stage is revalidated before resume.
7. Read the final natural-language response associated with `submit`.
8. Use DeepSeek to extract SROs and generate rewrite candidates.
9. Accept the first candidate satisfying the keyed watermark predicate, or
   reject the instance when the attempt budget is exhausted.
10. Export accepted, rejected, and ineligible records.

The keyed predicate hashes only the canonical task SRO and canonical response
SRO. Instance IDs, algorithm labels, timestamps, trajectory/step metadata, and
the original response text are not part of the HMAC message.

Dataset partitions are written as expanded JSON arrays in `accepted.json`,
`rejected.json`, `ineligible.json`, and `error.json`. They are not emitted as
single-line JSONL records.

`task_source: local_production` is the normal mode. The `local_static` and `existing`
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

For production runs, `tasks/task_production.json` records the SWE-smith commands,
pre/post bug counts, validation selection, local image name, and the exact new
instances passed to SWE-agent. It does not contain trajectory or patch hashes.

After SWE-agent returns, `trajectories/batch_validation.json` records the
per-instance batch status and trajectory path. `agent_execution` is marked
successful only after this report succeeds; the report path is also stored in
the run manifest.

## Required environment variables

- `DEEPSEEK_API_KEY`: DeepSeek API credential.
- `WM_SECRET_KEY`: secret used by the watermark predicate.
- Any credentials already required by the selected SWE-smith generator and
  SWE-agent configuration. The current local generator uses the same
  `DEEPSEEK_API_KEY`; Modal credentials are not used.

Secrets are never included in run manifests or exported dataset records.
