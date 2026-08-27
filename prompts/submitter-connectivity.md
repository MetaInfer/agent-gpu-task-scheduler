# Submitter Connectivity Check v1

Use only the configured submitter MCP tools. Create exactly one Proposal requesting 1 GPU, then stop immediately after `create_proposal` returns. Do not confirm the revision, submit a reply, poll for a Task, or call any cancellation tool.

The Proposal MUST use this exact heading order, with complete content and no `TBD`:

```markdown
# Proposal
## Identity
## Objective
## Success Criteria
## Workload and Code
## Container
## Resources
## Commands
## Inputs and Mounts
## Environment
## Networking and Privileges
## Timeout and Cleanup
## Framework Logs
## Business Logs and Outputs
## Multi-node Coordination
## Risks and Notes
```

The Proposal MUST state:

- submitter username `zz_chentian`;
- Worker `worker-local-01`;
- container `fh-sglang-deepseek-v4-flash`, container user `root`;
- image `harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238`;
- launcher `/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh` with SHA-256 `c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c`, invoked with exactly two positional arguments — the output path then the business log path — and no flags, because the launcher derives the world size from the injected `HIP_VISIBLE_DEVICES`;
- one Proposal-unique output `/data/agent-scheduler-mvp/outputs/<proposal-id>.json` and business log `/data/agent-scheduler-mvp/logs/<proposal-id>.log` (the Processor receives the actual proposal ID and must normalize these paths);
- a bounded foreground `torchrun` correctness smoke and 600-second total timeout.

Use a fresh unique idempotency key for `create_proposal`. Preserve the returned Proposal ID only for your final report. A successful `create_proposal` call is the end of this connectivity check; any further submitter MCP call is out of scope.
