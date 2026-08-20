"""Correctness-only ROCm all_reduce and GEMM qualification workload."""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

import torch
import torch.distributed as dist


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--business-log", required=True)
    parser.add_argument("--size", type=int, default=256)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise RuntimeError("ROCm/CUDA device support is required; CPU fallback is forbidden")
    if torch.version.hip is None:
        raise RuntimeError("qualification requires a ROCm PyTorch build")

    rank = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    if world_size not in {1, 2, 4, 8}:
        raise RuntimeError(f"unsupported qualification world size: {world_size}")
    if torch.cuda.device_count() != world_size:
        raise RuntimeError(
            f"visible device count {torch.cuda.device_count()} != world size {world_size}"
        )

    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    dist.init_process_group(backend="nccl", init_method="env://")
    try:
        collective = torch.tensor([rank + 1.0], device=device)
        dist.all_reduce(collective, op=dist.ReduceOp.SUM)
        expected = world_size * (world_size + 1) / 2
        actual = collective.item()
        if actual != expected:
            raise RuntimeError(f"all_reduce mismatch: {actual} != {expected}")

        left = torch.full((args.size, args.size), rank + 1.0, device=device)
        right = torch.eye(args.size, device=device)
        torch.cuda.synchronize(device)
        started = time.perf_counter()
        product = left @ right
        torch.cuda.synchronize(device)
        elapsed = time.perf_counter() - started
        if not torch.allclose(product, left, rtol=1e-4, atol=1e-4):
            raise RuntimeError("GEMM numerical check failed")

        rank_record = {
            "rank": rank,
            "local_rank": local_rank,
            "device_name": torch.cuda.get_device_name(local_rank),
            "all_reduce_value": actual,
            "gemm_elapsed_seconds": elapsed,
        }
        gathered: list[dict[str, object] | None] = [None] * world_size
        dist.all_gather_object(gathered, rank_record)
        dist.barrier()
        if rank == 0:
            output_path = Path(args.output)
            log_path = Path(args.business_log)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            result = {
                "schema_version": "v1",
                "task_id": os.environ["TASK_ID"],
                "unit_id": os.environ["UNIT_ID"],
                "execution_id": os.environ["EXECUTION_ID"],
                "plan_id": os.environ["PLAN_ID"],
                "assignment_id": os.environ["ASSIGNMENT_ID"],
                "lease_epoch": int(os.environ["LEASE_EPOCH"]),
                "gpu_ids": [int(value) for value in os.environ["HIP_VISIBLE_DEVICES"].split(",")],
                "backend": dist.get_backend(),
                "rocm_version": torch.version.hip,
                "world_size": world_size,
                "hostname": socket.gethostname(),
                "all_reduce": "ok",
                "gemm": "ok",
                "ranks": gathered,
            }
            output_path.write_text(
                json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            log_path.write_text(
                f"world_size={world_size} all_reduce=ok gemm=ok backend={dist.get_backend()}\n",
                encoding="utf-8",
            )
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
