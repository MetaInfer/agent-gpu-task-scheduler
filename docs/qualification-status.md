# MVP Qualification Status

- Checked at: 2026-08-21
- Status: `COMPLETED`
- Qualification run: `qual_01a0222d00687737b2b2d5795844311b`
- Code gate: 55 local tests passed, 3 real-environment tests opt-in/skipped; Ruff and Mypy clean
- Real-environment gates: `RUN_REAL_CLAUDE=1` and `RUN_REAL_GPU=1` both passed before the run

## Result

The qualification runner drove a real Claude Submitter through the local MCP adapter
against the loopback control plane, and all four current-run Tasks reached
`COMPLETED`:

| Cards | Proposal | Task | State |
| --- | --- | --- | --- |
| 1 | `prop_01a0222d88c7743aa4dc3f5bdb173893` | `task_01a022331478723fa7b2fad19a8ebf3d` | COMPLETED |
| 2 | `prop_01a0222dcdd27e8f9b94ae3c26cb51a2` | `task_01a022338dfc79669d220f86a328558c` | COMPLETED |
| 4 | `prop_01a0222e0b8172039151f39b003e7d4b` | `task_01a022302b9978a58c70c70e2b712835` | COMPLETED |
| 8 | `prop_01a0222e4cb476489a0e14b1e8afe79a` | `task_01a02233ed2e7f7daf56dd1c10d86370` | COMPLETED |

## Numerical evidence

Every output artifact is bound to its `task_id`, `unit_id`, `execution_id`, `plan_id`,
`assignment_id`, `lease_epoch`, and `gpu_ids`, and carries one record per rank.
Backend `nccl`, ROCm `6.3.26113`, device `K100_AI`.

| World size | GPU IDs | all_reduce | Expected Σ1..N | GEMM |
| --- | --- | --- | --- | --- |
| 1 | 0 | 1.0 | 1 | ok |
| 2 | 0-1 | 3.0 | 3 | ok |
| 4 | 0-3 | 10.0 | 10 | ok |
| 8 | 0-7 | 36.0 | 36 | ok |

## Negotiation evidence

The 4-GPU Proposal was approved on its first revision. The 1-, 2-, and 8-GPU Proposals
received `REQUEST_CHANGES` from the independent Reviewer over an artifact-path mismatch
between the `/data` container write paths in argv and the `/public/share`
`required_outputs`/`required_logs` paths. The Submitter fetched the reviews, issued
complete replacement revisions declaring the `/public/share/agent-scheduler-mvp ->
/data/agent-scheduler-mvp` bind mount, and explicitly confirmed each new revision with a
unique idempotency key. The review loop is therefore exercised, not bypassed.

## Post-conditions

Verified independently of the verifier's own report:

```text
fh-sglang-deepseek-v4-flash: Status=exited Running=false
residual leases: 0
/health integrity: valid
```

No package installation, image pull, container recreation, or dependency modification was
performed. The container's missing `librocm_smi64.so.2` was resolved by prepending the
image's own `/opt/dtk/.hyhal/rocm_smi/lib` to the frozen `LD_LIBRARY_PATH`.

## Profile note

The qualification profile's VRAM ceiling was raised from 90% to 97% by operator decision
while co-tenant containers held ~91% of each card. Those tenants released their memory
before the run, so admission actually sampled `VRAM% = 0` on all eight GPUs and the
relaxed ceiling was not load-bearing for this result. Tightening it back is safe. The
production default remains `< 2%`.
