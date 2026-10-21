#!/usr/bin/env python3
"""Build a Qwen3/LlamaFactory SFT dataset from accepted watermark trajectories.

The output training records intentionally contain only ``messages``. Audit metadata
such as instance IDs, task SROs, sampling decisions, and watermark keys is written
only to the separate build report and never enters the training JSONL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

INSTANCE_ID_RE = re.compile(
    r"^(?P<repository>.+)\.(?P<commit>[^.]+)\.(?P<generator>.+)__(?P<suffix>[^_]+)$"
)
SECRET_PATTERNS = {
    "github_token": re.compile(
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
FORBIDDEN_METADATA_FIELDS = {
    "derived_trajectory",
    "key_id",
    "problem_statement",
    "sampled_compressed_sentence",
    "sampled_response_sro",
    "sampled_terminal_response",
    "sampling",
    "source_trajectory",
    "task_sro",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", type=Path, required=True, help="Watermark pipeline run directory."
    )
    parser.add_argument(
        "--model-path", type=Path, required=True, help="Local Qwen3 model directory."
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New output directory."
    )
    parser.add_argument(
        "--cutoff-len",
        type=int,
        default=32768,
        help="Maximum prompt plus label tokens.",
    )
    parser.add_argument(
        "--template", default="qwen3_nothink", help="LlamaFactory template name."
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_instance_id(instance_id: str) -> tuple[str, str]:
    match = INSTANCE_ID_RE.fullmatch(instance_id)
    if match is None:
        raise ValueError(f"Malformed instance_id: {instance_id}")
    return match["repository"], match["generator"]


def resolve_derived_trajectory(run_dir: Path, relative_path: str) -> Path:
    candidate = (run_dir / relative_path).resolve()
    try:
        candidate.relative_to(run_dir)
    except ValueError as exc:
        raise ValueError(
            f"Derived trajectory escapes run directory: {relative_path}"
        ) from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"Missing derived trajectory: {candidate}")
    return candidate


def normalize_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("Tool arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise TypeError(
            f"Tool arguments must decode to an object, got {type(arguments).__name__}"
        )
    return arguments


def render_assistant_tool_message(message: dict[str, Any]) -> str:
    """Render mixed assistant text/tool calls in Qwen's chat-template form."""
    rendered = str(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise TypeError("Malformed assistant tool call")
        payload = {
            "name": function["name"],
            "arguments": normalize_arguments(function.get("arguments")),
        }
        if rendered:
            rendered += "\n"
        rendered += (
            "<tool_call>\n" + json.dumps(payload, ensure_ascii=False) + "\n</tool_call>"
        )
    return rendered


def build_messages(trajectory_data: dict[str, Any]) -> list[dict[str, str]]:
    steps = trajectory_data.get("trajectory")
    history = trajectory_data.get("history")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Trajectory has no steps")
    if not isinstance(history, list):
        raise TypeError("Trajectory history is not a list")

    query = steps[-1].get("query")
    if not isinstance(query, list):
        raise TypeError("Final trajectory query is not a list")
    if query != history[: len(query)]:
        raise ValueError("Final query is not the prefix of history")
    if len(history) <= len(query):
        raise ValueError("History has no label after the final query")

    label = history[len(query)]
    if not isinstance(label, dict) or label.get("role") != "assistant":
        raise ValueError("The message after the final query is not an assistant label")
    label_content = label.get("content")
    if not isinstance(label_content, str) or not label_content.strip():
        raise ValueError("Final assistant label is empty")

    messages: list[dict[str, str]] = []
    for message in query:
        if not isinstance(message, dict):
            raise TypeError("Query contains a non-object message")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            raise TypeError(f"Message content for role {role!r} is not a string")
        if role == "assistant":
            content = render_assistant_tool_message(message)
        elif role not in {"system", "user", "tool"}:
            raise ValueError(f"Unsupported query role: {role!r}")
        messages.append({"role": role, "content": content})

    # The derived watermark is in label.content. Its original empty submit call
    # must not replace the response text in LlamaFactory's function converter.
    messages.append({"role": "assistant", "content": label_content})
    validate_message_sequence(messages)
    return messages


def validate_message_sequence(messages: list[dict[str, str]]) -> None:
    body = messages[1:] if messages and messages[0]["role"] == "system" else messages
    expected = {0: {"user", "tool"}, 1: {"assistant"}}
    for index, message in enumerate(body):
        if message["role"] not in expected[index % 2]:
            raise ValueError(
                f"Invalid role sequence at body index {index}: {message['role']}"
            )
    if not body or body[-1]["role"] != "assistant":
        raise ValueError("Dataset example does not end with an assistant label")


def aligned_for_encoding(
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, str]], str]:
    if messages and messages[0]["role"] == "system":
        system = messages[0]["content"]
        body = messages[1:]
    else:
        system = ""
        body = messages
    aligned = [
        {
            "role": "observation" if message["role"] == "tool" else message["role"],
            "content": message["content"],
        }
        for message in body
    ]
    return aligned, system


def token_stats(lengths: list[int]) -> dict[str, int | float]:
    ordered = sorted(lengths)
    if not ordered:
        return {"count": 0}

    def percentile(fraction: float) -> int:
        return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": int(statistics.median(ordered)),
        "p90": percentile(0.90),
        "p95": percentile(0.95),
        "max": ordered[-1],
    }


def scan_record(record: dict[str, Any]) -> Counter[str]:
    hits: Counter[str] = Counter()
    unexpected = set(record) - {"messages"}
    for field in unexpected & FORBIDDEN_METADATA_FIELDS:
        hits[f"field:{field}"] += 1
    serialized = json.dumps(record, ensure_ascii=False)
    for name, pattern in SECRET_PATTERNS.items():
        count = len(pattern.findall(serialized))
        if count:
            hits[f"pattern:{name}"] += count
    return hits


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    model_path = args.model_path.resolve()
    output_dir = args.output_dir.resolve()
    if args.cutoff_len <= 0:
        raise ValueError("--cutoff-len must be positive")
    if output_dir.exists():
        raise FileExistsError(f"Output directory already exists: {output_dir}")

    accepted_path = run_dir / "accepted.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
    if not isinstance(accepted, list):
        raise TypeError("accepted.json must contain a list")
    instance_ids = [row.get("instance_id") for row in accepted]
    if any(not isinstance(instance_id, str) for instance_id in instance_ids):
        raise ValueError("Every accepted row must contain a string instance_id")
    if len(set(instance_ids)) != len(instance_ids):
        raise ValueError("accepted.json contains duplicate instance IDs")

    from llamafactory.data.template import get_template_and_fix_tokenizer
    from llamafactory.hparams import DataArguments
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    template = get_template_and_fix_tokenizer(
        tokenizer,
        DataArguments(template=args.template, enable_thinking=False),
    )

    included: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    audit_hits: Counter[str] = Counter()
    all_repo: Counter[str] = Counter()
    all_generator: Counter[str] = Counter()
    included_repo: Counter[str] = Counter()
    included_generator: Counter[str] = Counter()
    excluded_repo: Counter[str] = Counter()
    excluded_generator: Counter[str] = Counter()
    all_lengths: list[int] = []
    included_lengths: list[int] = []

    for row in accepted:
        instance_id = row["instance_id"]
        repository, generator = parse_instance_id(instance_id)
        all_repo[repository] += 1
        all_generator[generator] += 1
        trajectory_path = resolve_derived_trajectory(run_dir, row["derived_trajectory"])
        if (
            trajectory_path.stem != instance_id
            or trajectory_path.parent.name != instance_id
        ):
            raise ValueError(
                f"Derived trajectory path does not match instance_id: {instance_id}"
            )
        trajectory_data = json.loads(trajectory_path.read_text(encoding="utf-8"))
        messages = build_messages(trajectory_data)
        expected_label = row.get("sampled_terminal_response")
        if (
            not isinstance(expected_label, str)
            or messages[-1]["content"] != expected_label
        ):
            raise ValueError(
                f"Derived label does not match accepted terminal response: {instance_id}"
            )
        aligned, system = aligned_for_encoding(messages)
        prompt_ids, answer_ids = template.encode_oneturn(
            tokenizer, aligned, system=system, tools=""
        )
        prompt_tokens = len(prompt_ids)
        label_tokens = len(answer_ids)
        total_tokens = prompt_tokens + label_tokens
        all_lengths.append(total_tokens)
        if total_tokens > args.cutoff_len:
            excluded_repo[repository] += 1
            excluded_generator[generator] += 1
            exclusions.append(
                {
                    "instance_id": instance_id,
                    "repository": repository,
                    "generator": generator,
                    "prompt_tokens": prompt_tokens,
                    "label_tokens": label_tokens,
                    "total_tokens": total_tokens,
                    "reason": "exceeds_cutoff_len",
                }
            )
            continue

        record = {"messages": messages}
        audit_hits.update(scan_record(record))
        included.append(record)
        included_lengths.append(total_tokens)
        included_repo[repository] += 1
        included_generator[generator] += 1

    if audit_hits:
        raise ValueError(f"Privacy audit failed: {dict(audit_hits)}")

    output_dir.mkdir(parents=True)
    dataset_path = output_dir / "train.jsonl"
    with dataset_path.open("w", encoding="utf-8") as handle:
        for record in included:
            handle.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )

    dataset_name = "watermark_sft_qwen3_nothink"
    dataset_info = {
        dataset_name: {
            "file_name": dataset_path.name,
            "formatting": "sharegpt",
            "columns": {"messages": "messages"},
            "tags": {
                "role_tag": "role",
                "content_tag": "content",
                "user_tag": "user",
                "assistant_tag": "assistant",
                "observation_tag": "tool",
                "function_tag": "function",
                "system_tag": "system",
            },
        }
    }
    dataset_info_path = output_dir / "dataset_info.json"
    write_json(dataset_info_path, dataset_info)

    training_contract = {
        "dataset": dataset_name,
        "dataset_dir": str(output_dir),
        "model_name_or_path": str(model_path),
        "template": args.template,
        "enable_thinking": False,
        "cutoff_len": args.cutoff_len,
        "train_on_prompt": False,
        "mask_history": True,
        "packing": False,
        "val_size": 0.0,
        "required_semantics": "Only the final assistant response contributes supervised loss.",
    }
    training_contract_path = output_dir / "training_contract.json"
    write_json(training_contract_path, training_contract)

    report = {
        "schema_version": 1,
        "source": {
            "run_dir": str(run_dir),
            "accepted_file": str(accepted_path),
            "accepted_sha256": sha256_file(accepted_path),
        },
        "encoding": {
            "model_path": str(model_path),
            "template": args.template,
            "enable_thinking": False,
            "cutoff_len": args.cutoff_len,
            "overlength_policy": "drop_without_truncation",
            "split_policy": "all_samples_train_no_eval_or_test_split",
        },
        "counts": {
            "accepted": len(accepted),
            "included": len(included),
            "excluded_overlength": len(exclusions),
            "invalid": 0,
        },
        "token_lengths": {
            "all": token_stats(all_lengths),
            "included": token_stats(included_lengths),
        },
        "distribution": {
            "all": {
                "repository": dict(sorted(all_repo.items())),
                "generator": dict(sorted(all_generator.items())),
            },
            "included": {
                "repository": dict(sorted(included_repo.items())),
                "generator": dict(sorted(included_generator.items())),
            },
            "excluded": {
                "repository": dict(sorted(excluded_repo.items())),
                "generator": dict(sorted(excluded_generator.items())),
            },
        },
        "exclusions": sorted(
            exclusions, key=lambda item: item["total_tokens"], reverse=True
        ),
        "privacy_audit": {
            "passed": True,
            "training_record_keys": ["messages"],
            "message_keys": ["content", "role"],
            "forbidden_metadata_fields_present": [],
            "secret_pattern_hits": {},
        },
        "artifacts": {
            "dataset": dataset_path.name,
            "dataset_sha256": sha256_file(dataset_path),
            "dataset_info": dataset_info_path.name,
            "dataset_info_sha256": sha256_file(dataset_info_path),
            "training_contract": training_contract_path.name,
            "training_contract_sha256": sha256_file(training_contract_path),
        },
    }
    report_path = output_dir / "build_report.json"
    write_json(report_path, report)

    print(
        json.dumps(
            {"output_dir": str(output_dir), **report["counts"]}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
