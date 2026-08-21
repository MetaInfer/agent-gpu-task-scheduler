---
name: submit-gpu-task
description: Submit a GPU task to the Agent GPU Task Scheduler through the submitter MCP server - use when asked to run a GPU workload, submit a Proposal, request 1/2/4/8 cards, or drive a Task to completion on worker-local-01.
---

# Submitting a GPU task

Drive one Proposal from creation to a terminal Task state using **only** the
`mcp__submitter__*` tools. REST is the authority; never edit Ground Truth files directly.

## Before you start

Confirm the Master is up. If any tool call fails to connect, stop and tell the user to start
`agent-scheduler serve` and `agent-scheduler worker` — do not retry in a loop.

## Idempotency keys

Every `create_proposal`, `reply`, and `confirm_revision` call needs a **fresh unique** key.
Reusing a key with different content returns `409 IDEMPOTENCY_CONFLICT`. Use
`<action>-<short-purpose>-<counter>`, e.g. `create-gemm-8card-1`, `confirm-gemm-8card-2`.

## Workflow

1. **Write the Proposal.** All 15 sections, exact order, complete content, no `TBD`.
   Read `reference/proposal-template.md` and fill it in — do not improvise the structure.
2. **`create_proposal`** → returns `proposal.proposal_id` and `proposal.current_revision_id`.
3. **`confirm_revision`** with that `revision_id` to send it to the independent Reviewer.
4. **Handle the response:**
   - `200` → a Task was created. Go to step 5.
   - `409 CHANGES_REQUESTED` → call `get_reviews`, read the rationale, then `reply` with a
     **complete replacement** revision (not a diff, not a patch) that addresses it, and
     `confirm_revision` the new `revision_id` with a new key. Expect this; it is normal.
   - `409 REJECTED` / `ROUND_LIMIT` / `REVIEW_LIMIT` → stop and report. Do not resubmit.
   - `422` → your content is wrong. Read `message`, fix it, retry. Retrying unchanged is futile.
5. **Poll** with `wait_for_task` until the state is terminal. It returns after at most 30s,
   so call it repeatedly. Terminal states: `COMPLETED`, `FAILED`, `CANCELLED`,
   `CLEANUP_FAILED`, `RECONCILIATION_REQUIRED`.
6. **Report honestly.** `COMPLETED` means the launcher exited 0 and required artifacts exist.
   Anything else is a failure — say so, quote `failure_reason`, and use `get_logs` to explain
   why. Never infer success from log text.

## The run command is frozen

The Processor normalizes your Proposal into Facts, and Facts are validated strictly. The
qualification launcher takes **exactly two positional arguments** — output path, then business
log path — and **no flags**. World size comes from the scheduler-injected
`HIP_VISIBLE_DEVICES`, never from argv. Emitting `--nproc-per-node`, `--output`, `--log`, or
any other option gets the Proposal rejected with `422`.

| Field | Value |
| --- | --- |
| launcher | `/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh` |
| sha256 | `c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c` |
| argv[0] | `/data/agent-scheduler-mvp/outputs/<proposal-id>.json` |
| argv[1] | `/data/agent-scheduler-mvp/logs/<proposal-id>.log` |

`/data` inside the container is the host's `/public/share`. You do not know the proposal ID
when writing the first revision — write the literal `<proposal-id>`; the Processor receives the
real ID and substitutes it.

## Constraints you must not violate

- GPU count must be 1, 2, 4, or 8. Worker is `worker-local-01`, container is
  `fh-sglang-deepseek-v4-flash`, container user `root`.
- Never propose installing software, pulling images, or changing resource policy.
- Never request background or daemonized processes; the command must be bounded and foreground.
- Never reuse a historical Proposal or Task ID.
- The container is strictly serial — one Task at a time. Queuing is expected, not a fault.
