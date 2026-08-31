# MVP Qualification Status

- Checked at: 2026-08-31
- Status: `COMPLETED`
- Qualification run: `qual_01a057065c137facaab56b3905dfeba7`
- Harness under test: Claude Code (Submitter consumed the built Agent Client Kit 0.2.0)
- Code gate: 307 zero-cost tests passed; Ruff clean; Mypy clean (40 files)
- Real-environment gates: `RUN_REAL_CLAUDE=1` and `RUN_REAL_GPU=1` both passed before the run
- Client Kit: `/public/share/fh/agent-gpu-task-scheduler/dist/agent-client-kit-0.2.0`
  (built from `main` at `14535d3`; verified in-place and from a fresh extraction)

## Result

The qualification runner drove a real Claude Submitter through the Kit's isolated client
workspace against the loopback control plane, and all four current-run Tasks reached
`COMPLETED`:

| Cards | Proposal | Task | State |
| --- | --- | --- | --- |
| 1 | `prop_01a0570772c87c7cbe40c9346fa4ba3f` | `task_01a05708453c779c9c9cc5aa476eed0b` | COMPLETED |
| 2 | `prop_01a057086f2a768087235098aa3cb373` | `task_01a0570ac77476b7ba688d69a7b34330` | COMPLETED |
| 4 | `prop_01a05708d14c7c8c959f53a8fbcbb1e0` | `task_01a0570b20bc7e3f8992f9b80dfda294` | COMPLETED |
| 8 | `prop_01a057093b6d750d95d7935c316edc08` | `task_01a0570ba5fc7ff1a4eb79c2de34078c` | COMPLETED |

## Numerical evidence

Every output artifact is bound to its `task_id`, `unit_id`, `execution_id`, `plan_id`,
`assignment_id`, `lease_epoch`, and `gpu_ids`, and carries one record per rank.
Backend `nccl`, device `K100_AI`, hostname `kme6`.

| World size | GPU IDs | all_reduce | Expected Σ1..N | GEMM |
| --- | --- | --- | --- | --- |
| 1 | 0 | 1.0 | 1 | ok |
| 2 | 0-1 | 3.0 | 3 | ok |
| 4 | 0-3 | 10.0 | 10 | ok |
| 8 | 0-7 | 36.0 | 36 | ok |

## Post-conditions

Verified independently of the verifier's own report:

```text
fh-sglang-deepseek-v4-flash: Status=exited Running=false
residual leases: 0
/health integrity: valid
```

## Platform repairs required to reach this result

The acceptance environment had drifted from the frozen qualification baseline. Each item
below blocked one attempt and was fixed before this COMPLETED run:

- The shared venv predated the client package; the server CLI imports
  `agent_scheduler_client` at module scope without declaring it as a dependency. The
  client wheel 0.2.0 was installed into the venv. **Open follow-up:** declare
  `agent-gpu-task-scheduler-client` as a server-package dependency so a fresh install
  does not crash (`src/agent_scheduler/cli/main.py:15`).
- `tls/certificate.pem` was missing from the state root; it was restored from
  `secrets/tls-certificate.pem` (pair verified with `openssl`) in `init_runtime`'s
  original layout (0640, group-synced).
- No Worker process was running; one was started and connected over WSS.
- The Master's `_sample_local_worker` loop died silently on one `hy-smi` failure,
  freezing worker heartbeats so queued Tasks could never dispatch
  (`QUALIFICATION_GPU_WAIT_EXPIRED`). The scheduler and sampler loops now survive any
  transient failure (`5606bb0`, `be6d3a5`).
- The reuse container existed only as `fh-sglang-deepseek-v4-flash-test`; it matches
  the frozen baseline exactly and was stopped and renamed to
  `fh-sglang-deepseek-v4-flash` per operator direction.
- Two runner-environment test assumptions were fixed along the way (`14535d3`,
  `a53dfb9`): PATH leakage into a no-repo-path assertion, and a ten-second `--help`
  subprocess timeout that a transient host-load spike could trip.

## Codex / pi / dsh

Not yet qualified on this date. Blocked on provider-side prerequisites, not on the
Client Kit: Codex requires an OpenAI-compatible endpoint serving `/v1/*` (the
configured gateway serves the API at root paths only); pi's requests are rejected by
the configured gateways (`403 Request not allowed` on the Anthropic path, timeouts on
the OpenAI path); dsh needs a valid `DEEPSEEK_API_KEY` (the configured key is rejected
by the provider).

## Previous run (2026-08-21)

- Checked at: 2026-08-21
- Status: `COMPLETED`
- Qualification run: `qual_01a0222d00687737b2b2d5795844311b`
- Code gate: 55 local tests passed, 3 real-environment tests opt-in/skipped; Ruff and Mypy clean
- Real-environment gates: `RUN_REAL_CLAUDE=1` and `RUN_REAL_GPU=1` both passed before the run

### Result

The qualification runner drove a real Claude Submitter through the local MCP adapter
against the loopback control plane, and all four current-run Tasks reached
`COMPLETED`:

| Cards | Proposal | Task | State |
| --- | --- | --- | --- |
| 1 | `prop_01a0222d88c7743aa4dc3f5bdb173893` | `task_01a022331478723fa7b2fad19a8ebf3d` | COMPLETED |
| 2 | `prop_01a0222dcdd27e8f9b94ae3c26cb51a2` | `task_01a022338dfc79669d220f86a328558c` | COMPLETED |
| 4 | `prop_01a0222e0b8172039151f39b003e7d4b` | `task_01a022302b9978a58c70c70e2b712835` | COMPLETED |
| 8 | `prop_01a0222e4cb476489a0e14b1e8afe79a` | `task_01a02233ed2e7f7daf56dd1c10d86370` | COMPLETED |

### Numerical evidence

Every output artifact is bound to its `task_id`, `unit_id`, `execution_id`, `plan_id`,
`assignment_id`, `lease_epoch`, and `gpu_ids`, and carries one record per rank.
Backend `nccl`, ROCm `6.3.26113`, device `K100_AI`.

| World size | GPU IDs | all_reduce | Expected Σ1..N | GEMM |
| --- | --- | --- | --- | --- |
| 1 | 0 | 1.0 | 1 | ok |
| 2 | 0-1 | 3.0 | 3 | ok |
| 4 | 0-3 | 10.0 | 10 | ok |
| 8 | 0-7 | 36.0 | 36 | ok |

### Negotiation evidence

The 4-GPU Proposal was approved on its first revision. The 1-, 2-, and 8-GPU Proposals
received `REQUEST_CHANGES` from the independent Reviewer over an artifact-path mismatch
between the `/data` container write paths in argv and the `/public/share`
`required_outputs`/`required_logs` paths. The Submitter fetched the reviews, issued
complete replacement revisions declaring the `/public/share/agent-scheduler-mvp ->
/data/agent-scheduler-mvp` bind mount, and explicitly confirmed each new revision with a
unique idempotency key. The review loop is therefore exercised, not bypassed.

### Post-conditions

Verified independently of the verifier's own report:

```text
fh-sglang-deepseek-v4-flash: Status=exited Running=false
residual leases: 0
/health integrity: valid
```

No package installation, image pull, container recreation, or dependency modification was
performed. The container's missing `librocm_smi64.so.2` was resolved by prepending the
image's own `/opt/dtk/.hyhal/rocm_smi/lib` to the frozen `LD_LIBRARY_PATH`.

### Profile note

The qualification profile's VRAM ceiling was raised from 90% to 97% by operator decision
while co-tenant containers held ~91% of each card. Those tenants released their memory
before the run, so admission actually sampled `VRAM% = 0` on all eight GPUs and the
relaxed ceiling was not load-bearing for this result. Tightening it back is safe. The
production default remains `< 2%`.
