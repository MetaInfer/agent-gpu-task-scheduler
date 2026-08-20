#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON_SCRIPT=/data/fh/agent-gpu-task-scheduler/scripts/torch_collective_smoke.py
readonly PYTHON_SHA256=bff84ddef7d49c5347211ef619a2f6b5e7dcb21c06eef99a9736f7849015affc

if [[ $# -ne 2 ]]; then
  printf 'usage: %s OUTPUT BUSINESS_LOG\n' "$0" >&2
  exit 64
fi

printf '%s  %s\n' "$PYTHON_SHA256" "$PYTHON_SCRIPT" | sha256sum -c -
IFS=',' read -r -a gpu_ids <<< "${HIP_VISIBLE_DEVICES:?HIP_VISIBLE_DEVICES is required}"
world_size=${#gpu_ids[@]}
if [[ "$world_size" != 1 && "$world_size" != 2 && "$world_size" != 4 && "$world_size" != 8 ]]; then
  printf 'unsupported world size: %s\n' "$world_size" >&2
  exit 65
fi

exec torchrun \
  --standalone \
  --nnodes=1 \
  --nproc-per-node="$world_size" \
  "$PYTHON_SCRIPT" \
  --output "$1" \
  --business-log "$2"
