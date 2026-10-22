#!/usr/bin/env bash
set -euo pipefail

readonly WORKSPACE_ROOT="/mnt/sda/kei/agent-wm"
readonly CONFIG_PATH="${WORKSPACE_ROOT}/SWE-smith/configs/train/qwen3_8b_watermark_qlora.yaml"
readonly OUTPUT_DIR="/mnt/sda/kei/training-runs/qwen3-8b-watermark-qlora-v2"
readonly PYTHON_BIN="${WORKSPACE_ROOT}/.venv/bin/python"
readonly LLAMAFACTORY_CLI="${WORKSPACE_ROOT}/.venv/bin/llamafactory-cli"

if [[ ! -x "${PYTHON_BIN}" || ! -x "${LLAMAFACTORY_CLI}" ]]; then
  echo "Repository-root virtual environment is incomplete." >&2
  exit 1
fi
if [[ -e "${OUTPUT_DIR}" && ! -d "${OUTPUT_DIR}" ]]; then
  echo "Output path exists and is not a directory: ${OUTPUT_DIR}" >&2
  exit 1
fi
if [[ -d "${OUTPUT_DIR}" ]]; then
  shopt -s nullglob dotglob
  output_entries=("${OUTPUT_DIR}"/*)
  shopt -u nullglob dotglob
  if (( ${#output_entries[@]} > 0 )); then
    echo "Refusing to overwrite non-empty output directory: ${OUTPUT_DIR}" >&2
    exit 1
  fi
fi
if ! "${PYTHON_BIN}" -c 'import bitsandbytes' >/dev/null 2>&1; then
  echo "bitsandbytes is required for the configured 4-bit QLoRA run." >&2
  exit 1
fi
if ! "${PYTHON_BIN}" -c 'import liger_kernel' >/dev/null 2>&1; then
  echo "liger-kernel is required for memory-efficient long-context loss." >&2
  exit 1
fi

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"
export TOKENIZERS_PARALLELISM="false"

exec "${LLAMAFACTORY_CLI}" train "${CONFIG_PATH}"
