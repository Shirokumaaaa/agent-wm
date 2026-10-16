# Terminal Response Watermark Pipeline

This is an external orchestration project. It does not import or modify the
internals of SWE-smith or SWE-agent. The two projects are invoked through their
existing command-line interfaces.

The production pipeline runs these stages:

1. Select the first `local_repository_limit` entries from
   `swesmith.local_repositories` and distribute the global `max_bugs` attempt
   count across them.
2. For each selected repository, snapshot its existing SWE-smith bug artifact
   IDs, invoke the local bug generator, and collect only artifacts created by
   the current run.
3. Invoke SWE-smith's local Docker validation harness and retain candidates
   with non-empty `FAIL_TO_PASS` and `PASS_TO_PASS` sets.
4. Invoke the local issue generator, then build one run-scoped Docker image per
   successful repository containing all retained bug branches for that
   repository. No branch is pushed to a Git remote.
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

## SWE-smith generation plans

Local production supports all SWE-smith bug-generation families through a typed
generation plan:

- Direct generators: `lm_modify`, `lm_rewrite`, `procedural`, and `mirror`.
- Validated-patch combiners: `combine_file` and `combine_module`.

Direct generators run first and their run-owned artifacts are validated. Combiners
then receive an explicit allowlist containing only validated artifacts from their
configured `sources`; combined patches are collected and validated a second time.
Historical files already present below `SWE-smith/logs/bug_gen` are never silently
adopted by a new run.

The complete schema is demonstrated in
`configs/all_generators.example.yaml`. The original `local_generator`,
`local_generator_config`, `local_generator_model`, and
`local_generation_workers` fields remain supported and are converted to a
single-generator plan when `generators` is absent.

Each generator has its own global `budget`, distributed across the selected local
repositories. `task_limit` remains the final cap after validation and issue
generation. A mirror input is a SWE-bench-style JSON or JSONL file; records are
grouped by repository and a run-scoped exact profile map prevents interactive
revision selection. Repositories with no matching mirror input are skipped for
that mirror job. Mirror tasks can retain their original PR issue text with
`mirror_issue_mode: original_pr` and use `mirror_issue_fallback: llm` or `static`
when the original text is unavailable.

The upstream capability boundaries still apply: PR mirror recovery currently
rewrites Python files, and the module combiner's path grouping is Python-oriented.

Production evidence now records generator/combiner jobs, exact commands, artifact
producer IDs, validation state, and parent artifact IDs for combined patches.

For example, five repositories and a total generation budget of thirty bugs are
configured as follows:

```yaml
swesmith:
  local_repositories:
    - owner-a__repo-a.1234abcd
    - owner-b__repo-b.5678efab
    - owner-c__repo-c.90abcdef
    - owner-d__repo-d.1357ace0
    - owner-e__repo-e.2468bdf1
  local_repository_limit: 5
  max_bugs: 30
  task_limit: 30
```

`max_bugs` is the number of generation attempts to start and is divided as
evenly as possible across the selected repositories; with the values above,
each receives six attempts. It is not a success requirement. `task_limit` only
caps the successful, validated tasks passed onward. Keep
`required_task_count: null` for best-effort operation, or set it only when a
strict number of surviving tasks is required. A repository entry can also be a
mapping with explicit `base_image`, `base_branch`, or `task_repo` overrides.

`tasks/task_production.json` is updated atomically while production runs. It
contains one logical `attempt_id` per requested attempt and separately tracks
generation, validation, issue generation, image construction, and selection
for SWE-agent. Failed attempts do not need a real SWE-smith `instance_id`.

The pipeline does not alter model sampling settings. It sends no temperature,
top-p, token-limit, or reasoning overrides to the sampler model, and it does
not rewrite the existing SWE-agent configuration.

## Entry point

Run from the repository root with the existing virtual environment:

```bash
.venv/bin/python watermark-pipeline/run.py \
  --config watermark-pipeline/configs/pipeline.yaml
```

Validate budgets, referenced files, repository allocation, and dependency wiring
without starting Docker or making model calls:

```bash
.venv/bin/python watermark-pipeline/run.py \
  --config watermark-pipeline/configs/pipeline.yaml \
  --plan-only
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
