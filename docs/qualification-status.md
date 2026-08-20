# MVP Qualification Status

- Checked at: 2026-08-21
- Status: `BLOCKED_QUALIFICATION`
- Code gate: 45 local tests passed, 3 real-environment tests opt-in/skipped; Ruff and Mypy passed before real preflight

## Blocking preconditions

1. **Claude authentication**: the process environment and project `.env` do not provide `ANTHROPIC_API_KEY`. The confirmed `claude --bare` contract intentionally does not use `ANTHROPIC_AUTH_TOKEN` or OAuth/keychain credentials.
2. **GPU admission**: `hy-smi` reported `VRAM%=92%` for GPU 0-7. The qualification profile requires a fresh sample with `VRAM% < 90%`; the scheduler must not bypass this gate.
3. **Container baseline**: after an authorized `start -> exec preflight -> stop`, importing PyTorch failed because `librocm_smi64.so.2` was unavailable. `torchrun` was present. Both versioned qualification files were visible through `/data` and matched their frozen SHA-256 values.

## Cleanup evidence

The authorized preflight ended with:

```text
fh-sglang-deepseek-v4-flash: exited / Running=false
```

No package installation, image pull, container recreation, or dependency modification was performed.

## Required administrator actions

- Provide `ANTHROPIC_API_KEY` to the parent process without placing it in Git, argv, logs, or chat.
- Make GPU 0-7 satisfy the approved `<90%` VRAM qualification profile.
- Repair the approved container baseline so its existing PyTorch build can load `librocm_smi64.so.2`; the scheduler will not install dependencies dynamically.

After all three are resolved, start Master and Worker as documented in `README.md`, then run `uv run agent-scheduler qualify`. Goal completion requires all 1/2/4/8-card evidence to pass; this document alone is not completion evidence.
