#!/usr/bin/env python3
"""Evaluate terminal-response watermark recovery for a base or SFT Qwen model.

The evaluator reproduces the fast-validation experiment from ``train_design.md``:
it gives the model exactly ``trajectory[-1]["query"]`` (as packaged for SFT),
generates only the final assistant response, and passes that response through the
unchanged production DeepSeek SRO extractor and watermark predicate.

Watermark keys and task SROs are used only in memory.  They are deliberately not
written to the per-example results or summary report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--pipeline-config", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument(
        "--adapter-path",
        type=Path,
        help="Optional PEFT/LoRA adapter to apply to the base model.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--llamafactory-dir",
        type=Path,
        default=Path("/mnt/sda/kei/LlamaFactory-0.9.5"),
    )
    parser.add_argument("--template", default="qwen3_nothink")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--selection",
        choices=("first", "shortest"),
        default="first",
        help="Subset ordering used with --limit.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append only missing selected instances to an interrupted result file.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="With --resume, remove prior error rows and evaluate them again.",
    )
    return parser.parse_args()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"{path}:{line_number} is not a JSON object")
            records.append(value)
    return records


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prompt_hashes(prompt_dir: Path) -> dict[str, str]:
    names = (
        "compress_terminal_response.txt",
        "extract_response_sro.txt",
        "extract_task_sro.txt",
        "rewrite_terminal_response.txt",
    )
    return {name: sha256_file(prompt_dir / name) for name in names}


def predicate_fingerprint(config: Any) -> str:
    """Fingerprint sensitive predicate settings without exposing their values."""
    payload = {
        "watermark": config.watermark,
        "code_key": config.code_key,
        "task_key": config.task_key,
        "response_key": config.response_key,
        "code_modulus": config.code_modulus,
        "modulus": config.modulus,
        "target": config.target,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def task_sro_from_row(row: dict[str, Any], sro_type: type) -> Any:
    value = row.get("task_sro")
    if not isinstance(value, dict):
        raise TypeError(f"Missing task_sro for {row.get('instance_id')}")
    try:
        return sro_type(
            subject=str(value["subject"]),
            relation=str(value["relation"]),
            object=str(value["object"]),
        )
    except KeyError as exc:
        raise ValueError(f"Malformed task_sro for {row.get('instance_id')}") from exc


def accepted_response_sro_from_row(row: dict[str, Any], sro_type: type) -> Any:
    value = row.get("sampled_response_sro")
    if not isinstance(value, dict):
        raise TypeError(f"Missing sampled_response_sro for {row.get('instance_id')}")
    try:
        return sro_type(
            subject=str(value["subject"]),
            relation=str(value["relation"]),
            object=str(value["object"]),
        )
    except KeyError as exc:
        raise ValueError(
            f"Malformed sampled_response_sro for {row.get('instance_id')}"
        ) from exc


def validate_inputs(
    *,
    accepted: list[dict[str, Any]],
    records: list[dict[str, Any]],
    build_report: dict[str, Any],
    predicate: Any,
    algorithm: str,
    key_id: str,
    sro_type: type,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    exclusions = build_report.get("exclusions", [])
    excluded_ids = {
        item["instance_id"]
        for item in exclusions
        if isinstance(item, dict) and isinstance(item.get("instance_id"), str)
    }
    included = [row for row in accepted if row.get("instance_id") not in excluded_ids]
    if len(included) != len(records):
        raise ValueError(
            f"Included accepted rows ({len(included)}) do not match dataset records "
            f"({len(records)})"
        )

    paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row, record in zip(included, records, strict=True):
        sampling = row.get("sampling")
        if not isinstance(sampling, dict):
            raise TypeError(f"Missing sampling metadata for {row.get('instance_id')}")
        if sampling.get("algorithm") != algorithm:
            raise ValueError(f"Algorithm mismatch for {row.get('instance_id')}")
        if sampling.get("key_id") != key_id:
            raise ValueError(f"Key ID mismatch for {row.get('instance_id')}")

        messages = record.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Missing messages for {row.get('instance_id')}")
        if messages[-1].get("role") != "assistant":
            raise ValueError(
                f"Dataset label is not assistant for {row.get('instance_id')}"
            )
        if messages[-1].get("content") != row.get("sampled_terminal_response"):
            raise ValueError(
                f"Dataset/accepted label mismatch for {row.get('instance_id')}"
            )

        task_sro = task_sro_from_row(row, sro_type)
        accepted_response_sro = accepted_response_sro_from_row(row, sro_type)
        if not predicate.accepts(task_sro=task_sro, response_sro=accepted_response_sro):
            raise ValueError(
                f"Stored accepted response fails predicate for {row.get('instance_id')}"
            )
        paired.append((row, record))
    return paired


def main() -> int:
    args = parse_args()
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit must be positive")
    if args.max_new_tokens <= 0:
        raise ValueError("--max-new-tokens must be positive")
    if args.retry_errors and not args.resume:
        raise ValueError("--retry-errors requires --resume")

    dataset_dir = args.dataset_dir.resolve()
    run_dir = args.run_dir.resolve()
    pipeline_config_path = args.pipeline_config.resolve()
    model_path = args.model_path.resolve()
    output_dir = args.output_dir.resolve()
    llamafactory_dir = args.llamafactory_dir.resolve()
    adapter_path = args.adapter_path.resolve() if args.adapter_path else None
    if output_dir.exists() and not args.resume:
        raise FileExistsError(f"Output directory already exists: {output_dir}")
    for directory in (dataset_dir, run_dir, model_path, llamafactory_dir):
        if not directory.is_dir():
            raise FileNotFoundError(directory)
    if adapter_path is not None and not adapter_path.is_dir():
        raise FileNotFoundError(adapter_path)

    workspace_root = Path(__file__).resolve().parents[2]
    pipeline_src = workspace_root / "watermark-pipeline" / "src"
    sys.path.insert(0, str(llamafactory_dir / "src"))
    sys.path.insert(0, str(pipeline_src))

    import torch
    from llamafactory.data.template import get_template_and_fix_tokenizer
    from llamafactory.hparams import DataArguments
    from prepare_watermark_sft import aligned_for_encoding
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from watermark_pipeline.config import PipelineConfig
    from watermark_pipeline.llm.deepseek import DeepSeekClient
    from watermark_pipeline.llm.prompts import PromptStore
    from watermark_pipeline.schemas import SRO
    from watermark_pipeline.watermark.predicate import ALGORITHM, WatermarkPredicate
    from watermark_pipeline.watermark.sro import SROExtractor

    pipeline_config = PipelineConfig.load(pipeline_config_path)
    sampler = pipeline_config.sampler
    predicate = WatermarkPredicate(
        watermark=sampler.watermark,
        code_key=sampler.code_key,
        task_key=sampler.task_key,
        response_key=sampler.response_key,
        code_modulus=sampler.code_modulus,
        modulus=sampler.modulus,
        target=sampler.target,
    )

    dataset_path = dataset_dir / "train.jsonl"
    build_report_path = dataset_dir / "build_report.json"
    build_report = read_json(build_report_path)
    expected_dataset_hash = build_report.get("artifacts", {}).get("dataset_sha256")
    actual_dataset_hash = sha256_file(dataset_path)
    if actual_dataset_hash != expected_dataset_hash:
        raise ValueError("Dataset SHA-256 does not match build_report.json")

    accepted = read_json(run_dir / "accepted.json")
    records = read_jsonl(dataset_path)
    if not isinstance(accepted, list):
        raise TypeError("accepted.json must contain a list")
    paired = validate_inputs(
        accepted=accepted,
        records=records,
        build_report=build_report,
        predicate=predicate,
        algorithm=ALGORITHM,
        key_id=sampler.key_id,
        sro_type=SRO,
    )

    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    template = get_template_and_fix_tokenizer(
        tokenizer,
        DataArguments(template=args.template, enable_thinking=False),
    )
    encoded: list[dict[str, Any]] = []
    for row, record in paired:
        messages = record["messages"]
        aligned, system = aligned_for_encoding(messages[:-1])
        prompt_ids, _ = template.encode_oneturn(
            tokenizer, aligned, system=system, tools=""
        )
        encoded.append(
            {
                "row": row,
                "prompt_ids": prompt_ids,
                "prompt_tokens": len(prompt_ids),
            }
        )
    if args.selection == "shortest":
        encoded.sort(
            key=lambda item: (item["prompt_tokens"], item["row"]["instance_id"])
        )
    if args.limit is not None:
        encoded = encoded[: args.limit]
    if not encoded:
        raise ValueError("No evaluation samples selected")

    output_dir.mkdir(parents=True, exist_ok=args.resume)
    results_path = output_dir / "results.jsonl"
    results = read_jsonl(results_path) if args.resume and results_path.exists() else []
    retry_count = 0
    if args.retry_errors:
        retained_results = [result for result in results if result.get("error") is None]
        retry_count = len(results) - len(retained_results)
        if retry_count:
            rewrite_jsonl(results_path, retained_results)
        results = retained_results
    selected_ids = {item["row"]["instance_id"] for item in encoded}
    completed_ids: set[str] = set()
    for result in results:
        instance_id = result.get("instance_id")
        if not isinstance(instance_id, str) or instance_id not in selected_ids:
            raise ValueError("Existing results do not match the current selection")
        if instance_id in completed_ids:
            raise ValueError(f"Duplicate existing result for {instance_id}")
        completed_ids.add(instance_id)
    pending = [
        item for item in encoded if item["row"]["instance_id"] not in completed_ids
    ]

    if not torch.cuda.is_available() and args.device.startswith("cuda"):
        raise RuntimeError("CUDA was requested but is unavailable")
    dtype = torch.bfloat16 if args.device.startswith("cuda") else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        local_files_only=True,
        torch_dtype=dtype,
        attn_implementation="sdpa",
    )
    if adapter_path is not None:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, adapter_path, is_trainable=False)
    model.to(args.device)
    model.eval()

    prompt_dir = pipeline_config.project_dir / "prompts"
    extractor = SROExtractor(
        DeepSeekClient(sampler),
        PromptStore(prompt_dir),
    )
    eos_token_ids = template.get_stop_token_ids(tokenizer)
    pad_token_id = tokenizer.pad_token_id
    if pad_token_id is None:
        pad_token_id = tokenizer.eos_token_id

    result_mode = "a" if results else "w"
    with results_path.open(result_mode, encoding="utf-8") as handle:
        for index, item in enumerate(pending, start=len(results) + 1):
            row = item["row"]
            input_ids = torch.tensor(
                [item["prompt_ids"]], dtype=torch.long, device=args.device
            )
            attention_mask = torch.ones_like(input_ids)
            try:
                with torch.inference_mode():
                    generated = model.generate(
                        input_ids=input_ids,
                        attention_mask=attention_mask,
                        do_sample=False,
                        max_new_tokens=args.max_new_tokens,
                        eos_token_id=eos_token_ids,
                        pad_token_id=pad_token_id,
                        use_cache=True,
                    )
                new_ids = generated[0, input_ids.shape[1] :]
                response = tokenizer.decode(new_ids, skip_special_tokens=True).strip()
                response_sro = extractor.from_response(response)
                eligible = response_sro.has_primary_sro()
                hit = eligible and predicate.accepts(
                    task_sro=task_sro_from_row(row, SRO),
                    response_sro=response_sro.primary_sro(),
                )
                result = {
                    "instance_id": row["instance_id"],
                    "prompt_tokens": item["prompt_tokens"],
                    "generated_tokens": int(new_ids.shape[0]),
                    "response": response,
                    "compressed_sentence": response_sro.compressed_sentence,
                    "response_sro": asdict(response_sro.primary_sro()),
                    "eligible": eligible,
                    "hit": hit,
                    "error": None,
                }
            # Model backends and LiteLLM expose backend-specific exception classes.
            # Preserve partial evidence instead of discarding a completed baseline.
            except Exception as exc:  # noqa: BLE001
                result = {
                    "instance_id": row["instance_id"],
                    "prompt_tokens": item["prompt_tokens"],
                    "generated_tokens": None,
                    "response": None,
                    "compressed_sentence": None,
                    "response_sro": None,
                    "eligible": False,
                    "hit": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            print(
                json.dumps(
                    {
                        "completed": index,
                        "total": len(encoded),
                        "instance_id": row["instance_id"],
                        "eligible": result["eligible"],
                        "hit": result["hit"],
                        "error": result["error"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    eligible_count = sum(bool(result["eligible"]) for result in results)
    hit_count = sum(bool(result["hit"]) for result in results)
    error_count = sum(result["error"] is not None for result in results)
    report = {
        "schema_version": 1,
        "experiment": "terminal_context_watermark_baseline",
        "input": {
            "dataset_dir": str(dataset_dir),
            "dataset_sha256": actual_dataset_hash,
            "run_dir": str(run_dir),
            "accepted_sha256": sha256_file(run_dir / "accepted.json"),
            "pipeline_config": str(pipeline_config_path),
        },
        "model": {
            "base_model": str(model_path),
            "adapter": str(adapter_path) if adapter_path is not None else None,
            "template": args.template,
            "enable_thinking": False,
            "device": args.device,
            "dtype": str(dtype),
            "generation": {
                "do_sample": False,
                "max_new_tokens": args.max_new_tokens,
            },
        },
        "decoder": {
            "model": sampler.model,
            "api_base": sampler.api_base,
            "generation_parameters": {},
            "prompt_sha256": prompt_hashes(prompt_dir),
        },
        "watermark": {
            "algorithm": ALGORITHM,
            "key_id": sampler.key_id,
            "code_modulus": sampler.code_modulus,
            "modulus": sampler.modulus,
            "target": sampler.target,
            "sensitive_config_sha256": predicate_fingerprint(sampler),
        },
        "selection": {
            "mode": args.selection,
            "limit": args.limit,
            "selected": len(encoded),
            "resumed": args.resume,
            "prior_errors_requeued": retry_count,
        },
        "preflight": {
            "packaged_samples": len(records),
            "stored_accepted_predicate_hits": len(paired),
            "metadata_mismatches": 0,
        },
        "results": {
            "completed": len(results),
            "eligible": eligible_count,
            "hits": hit_count,
            "errors": error_count,
            "hit_rate_all": hit_count / len(results),
            "hit_rate_eligible": (
                hit_count / eligible_count if eligible_count else None
            ),
        },
        "artifacts": {"results": results_path.name},
    }
    write_json(output_dir / "report.json", report)
    print(json.dumps(report["results"], ensure_ascii=False), flush=True)
    return 0 if error_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
