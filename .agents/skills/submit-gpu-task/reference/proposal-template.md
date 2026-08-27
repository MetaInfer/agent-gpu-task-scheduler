# Proposal template

Fill every section. Exact order, no renaming, no `TBD`. Replace `<N>` with 1, 2, 4, or 8 and
`<proposal-id>` literally — the Processor substitutes the real ID.

Read `<submitter-username>` (the configured submitter username) from the configured
`create_proposal` tool description and replace it exactly; do not submit the angle-bracket token
literally.

This template is derived from a Proposal that actually passed the independent Reviewer. The
common cause of `REQUEST_CHANGES` is leaving the container write path and the host verification
path looking like two different files, so **Inputs and Mounts** states the bind explicitly.

---

```markdown
# Proposal

## Identity
Submitter username: `<submitter-username>`.
This Proposal requests <N> GPUs.

## Objective
Run a bounded, foreground `torchrun` collective-communication correctness smoke test on a
single Worker using exactly <N> GPUs, verifying that the scheduler, container, launcher, and
artifact plumbing work end to end.

## Success Criteria
- The launcher exits 0 within the 600-second total timeout.
- Exactly one JSON result exists at the Proposal-unique output path, written in the container
  as `/data/agent-scheduler-mvp/outputs/<proposal-id>.json` and verified on the host as
  `/public/share/agent-scheduler-mvp/outputs/<proposal-id>.json` — the same file via the bind
  mount declared below.
- Exactly one business log exists, written as `/data/agent-scheduler-mvp/logs/<proposal-id>.log`
  and verified as `/public/share/agent-scheduler-mvp/logs/<proposal-id>.log` — again one file.
- The smoke reports world size <N>, matching the injected `HIP_VISIBLE_DEVICES`. A different
  world size is a failure even if the process exits 0.
- No background or daemonized processes remain.

## Workload and Code
A bounded PyTorch collective correctness smoke driven by `torchrun`. The launcher initializes
the process group, runs all-reduce and GEMM checks across the <N> ranks, validates the numeric
results against expected values, then writes a JSON summary and a business log. Short-running,
deterministic, no training, no downloads, no installation.

## Container
Container name: `fh-sglang-deepseek-v4-flash`
Container user: `root`
Image: `harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238`
The image already contains PyTorch and the DCU/ROCm runtime. Nothing is installed at runtime.

## Resources
Worker: `worker-local-01`
GPU count: <N> (GPU type `K100_AI`)
Worker count: 1. Single node, single container. No change to resource policy is requested.

## Commands
Launcher: `/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh`
Launcher SHA-256: `c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c`

Invocation uses exactly two positional arguments — output path first, then business log path —
and no flags:

    /data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh \
      /data/agent-scheduler-mvp/outputs/<proposal-id>.json \
      /data/agent-scheduler-mvp/logs/<proposal-id>.log

No flags are passed because the launcher derives world size from the injected
`HIP_VISIBLE_DEVICES`. The command runs in the foreground; the Task is complete when the
launcher exits.

## Inputs and Mounts
| Host source | Container target | Type | Direction |
| --- | --- | --- | --- |
| `/public/share` | `/data` | bind | read-write |

Consequences, stated explicitly so write and verification paths are provably one file:
- Container `/data/agent-scheduler-mvp/outputs` **is** host `/public/share/agent-scheduler-mvp/outputs`.
- Container `/data/agent-scheduler-mvp/logs` **is** host `/public/share/agent-scheduler-mvp/logs`.
- The mapping is a live bind, not a copy-on-completion export, so no post-run transfer step
  can fail independently of the workload.

No dataset, model weights, or network inputs are required.

## Environment
- `HIP_VISIBLE_DEVICES` is injected by the scheduler and is the sole source of truth for world
  size.
- No other environment overrides are requested.
- No proxy, credential, or registry variables are needed.

## Networking and Privileges
- Single-node run; only host-local loopback is used for `torchrun` rendezvous and collectives.
- No external network access required.
- No additional privileges or device passthrough beyond the scheduler-injected GPUs.
- Container user is `root`, per the standard container definition.

## Timeout and Cleanup
- Total timeout: 600 seconds, covering startup, the smoke, and result writing.
- On timeout or failure the launcher process group is terminated and the Task marked failed.
- Strictly foreground; no background processes remain. The output JSON and business log are
  intentionally retained on shared storage.

## Framework Logs
Framework stdout/stderr from the launcher and `torchrun` are captured by the scheduler and
retrievable per Task/unit/execution. They diagnose startup, rendezvous, mount, and exit-code
issues, and are distinct from the business log artifact.

## Business Logs and Outputs
Exactly one output and one business log per Proposal, each Proposal-unique:
- Output — container `/data/agent-scheduler-mvp/outputs/<proposal-id>.json`; host
  `/public/share/agent-scheduler-mvp/outputs/<proposal-id>.json`; same file.
- Business log — container `/data/agent-scheduler-mvp/logs/<proposal-id>.log`; host
  `/public/share/agent-scheduler-mvp/logs/<proposal-id>.log`; same file.

Success is judged from the launcher exit code plus the JSON contents, not from log text.

## Multi-node Coordination
Not applicable. Single-node, single-container on `worker-local-01`. All <N> ranks are local;
`torchrun` uses a standalone local rendezvous. No cross-node coordination.

## Risks and Notes
- Risk: `HIP_VISIBLE_DEVICES` missing or listing fewer than <N> devices, yielding a wrong world
  size. Mitigation: the JSON output records the observed world size; any value other than <N>
  is scored as failure regardless of exit code.
- Risk: artifact directory not writable by `root`. Mitigation: the launcher fails fast with a
  non-zero exit.
- Risk: launcher drift. Mitigation: the pinned SHA-256 is verified immediately before execution.
- No software is installed, no resource policy is changed, and no historical IDs are reused.
```

## Responding to REQUEST_CHANGES

Call `get_reviews`, read `rationale`, then send a **complete replacement** revision through
`reply` — the whole document again with the issue fixed, never a diff. Add a sentence to
`## Identity` naming what you changed, and expand the section the Reviewer criticized rather
than only restating it.
