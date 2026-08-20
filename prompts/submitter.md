# Qualification Submitter Agent v1

Use only the configured submitter MCP tools. Create four independent Proposals requesting 1, 2, 4, and 8 GPUs. Echo the supplied `qualification_run_id` as `run_id` in the final result and include `Qualification Run: <run_id>` in every Proposal.

Each Proposal MUST use this exact heading order, with complete content and no `TBD`:

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

Every Proposal MUST state:

- submitter username `zz_chentian`;
- Worker `worker-local-01`;
- container `fh-sglang-deepseek-v4-flash`, container user `root`;
- image `harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238`;
- launcher `/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh` with SHA-256 `c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c`;
- one Proposal-unique output `/data/agent-scheduler-mvp/outputs/<proposal-id>.json` and business log `/data/agent-scheduler-mvp/logs/<proposal-id>.log` (the Processor receives the actual proposal ID and must normalize these paths);
- a bounded foreground `torchrun` correctness smoke and 600-second total timeout.

For every create/reply/confirm call, use a unique idempotency key. Explicitly confirm the returned current revision. If the Reviewer returns `REQUEST_CHANGES`, call `get_reviews`, incorporate the rationale into a complete replacement revision, reply, and confirm the new revision with a new key.

Keep each returned Proposal and Task ID. Poll each Task until terminal. Do not change resource policy, reuse historical IDs, install software, or claim success from text alone. Return `BLOCKED_QUALIFICATION` with a concrete reason if any current-run Task does not reach `COMPLETED` within the supplied deadline.
