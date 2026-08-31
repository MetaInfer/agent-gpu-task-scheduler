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

## dsh

- Checked at: 2026-08-31
- Status: `COMPLETED`
- Qualification run: `qual_01a05723f00c74d29f2a6434cf732b52`
- Harness under test: dsh (headless profile, `DSH_PERMISSION_MODE=danger-full-access` for
  this invocation only), credentials from the operator-provided `.env`
- T2 (fake-Master connectivity): passed (24.5s)

| Cards | Proposal | Task | State |
| --- | --- | --- | --- |
| 1 | `prop_01a05724df5b75a4ac4d0b772957c3b9` | `task_01a05726ae1f75cc90a6ff6d018ee361` | COMPLETED |
| 2 | `prop_01a0572537e27d1c9fd29b4a1bcde851` | `task_01a05726eb6b786aa9d81e9a4b6e00af` | COMPLETED |
| 4 | `prop_01a0572592dc7a97a2dd1b2422114e78` | `task_01a0572724b27659984d191f26ab391b` | COMPLETED |
| 8 | `prop_01a05725ffde7b1e9ff631e6f1386ac3` | `task_01a057276ff675eea1e23a0017251720` | COMPLETED |

Numerical evidence: all_reduce 1.0/3.0/10.0/36.0 on 1/2/4/8 cards (`gpu_ids`
0 / 0-1 / 0-3 / 0-7), GEMM ok, backend nccl, host kme6.

## pi

- Checked at: 2026-08-31
- Status: `COMPLETED`
- Qualification run: `qual_01a057c471ed7849a2c28282b76f12f7`
- Harness under test: pi through `--provider deepseek --model deepseek-v4-flash`
  (`AGENT_SCHEDULER_PI_PROVIDER=deepseek`, `AGENT_SCHEDULER_PI_MODEL=deepseek-v4-flash`);
  the Anthropic and OpenAI gateways reject pi's request shape, so the deepseek
  provider with the operator-provided `.env` credentials is the qualified path
- T2 (fake-Master connectivity): passed (31.8s)

| Cards | Proposal | Task | State |
| --- | --- | --- | --- |
| 1 | `prop_01a057c678977f86b8672d8410a9179e` | `task_01a057c85d2a715a8d380ba54396e97c` | COMPLETED |
| 2 | `prop_01a057c6acf57113a1a4d193ff6b03ce` | `task_01a057c8a50d75b2ae74581cc87edd5e` | COMPLETED |
| 4 | `prop_01a057c70adc74ae834ae442de62f880` | `task_01a057c8e2f47e2fad7902b0587af6ba` | COMPLETED |
| 8 | `prop_01a057c7a1c77f889d34a3915f7bdae4` | `task_01a057c91f777064b3a4a72d5ebecb4a` | COMPLETED |

Numerical evidence: all_reduce 1.0/3.0/10.0/36.0 on 1/2/4/8 cards (`gpu_ids`
0 / 0-1 / 0-3 / 0-7), GEMM ok, backend nccl.

## Codex

- Checked at: 2026-08-31
- Status: `COMPLETED`
- Qualification run: `qual_01a057d7a21d7545968eef1fa7c6a9f9`
- Harness under test: Codex CLI through the operator's `/v1` gateway endpoint
  (`model_provider` config inherited from the real `CODEX_HOME`, model gpt-5.6-sol)
- T2 (fake-Master connectivity): passed (1:54)

| Cards | Proposal | Task | State |
| --- | --- | --- | --- |
| 1 | `prop_01a057d9689973b3ba1be3cbb6288243` | `task_01a057db524c7b13b1027707bbf46b53` | COMPLETED |
| 2 | `prop_01a057d9c5e37ad7b442657cedaf8f2c` | `task_01a057db93e87c9cb58f926ae47ae2eb` | COMPLETED |
| 4 | `prop_01a057da042e792eb76a6b7e92ca6198` | `task_01a057dc13b17c76b3ee15e2810827de` | COMPLETED |
| 8 | `prop_01a057da5fde7e5fa8f11011e9bf2428` | `task_01a057dbde367ead9c5c47507e26f15b` | COMPLETED |

Numerical evidence: all_reduce 1.0/3.0/10.0/36.0 on 1/2/4/8 cards (`gpu_ids`
0 / 0-1 / 0-3 / 0-7), GEMM ok, backend nccl.

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
