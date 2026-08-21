# MVP Qualification Status

- Checked at: 2026-08-21
- Status: `BLOCKED_QUALIFICATION`
- Code gate: 50 local tests passed, 3 real-environment tests opt-in/skipped; Ruff and Mypy passed before real preflight

## Blocking preconditions

1. **Claude authentication**: resolved by operator approval. The real-role contract is now restricted non-bare headless (`claude --print --setting-sources ""`, built-in tools / slash commands / session persistence / external settings all disabled) and accepts either `ANTHROPIC_API_KEY` or the existing `ANTHROPIC_AUTH_TOKEN` from the parent process environment.
2. **GPU admission**: `hy-smi` reported `VRAM%=91%` for GPU 0-7, held by co-tenant containers on the shared node. The operator raised the qualification ceiling to `VRAM% < 97%`, so admission now passes; the scheduler still must not bypass or auto-raise this gate.
3. **Container baseline resolved**: the initial import missed `librocm_smi64.so.2`, but the approved image already contains it under `/opt/dtk/.hyhal/rocm_smi/lib`. With that existing directory prepended to the frozen `LD_LIBRARY_PATH`, PyTorch 2.9.0 imported successfully, reported ROCm 6.3.26113, 8 visible GPUs, and `cuda.is_available() == true`. No package or image change was made. `torchrun` and both versioned qualification files are present with matching SHA-256 values.

## Cleanup evidence

The authorized preflight ended with:

```text
fh-sglang-deepseek-v4-flash: exited / Running=false
```

No package installation, image pull, container recreation, or dependency modification was performed.

## Required administrator actions

- Export `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` into the parent process without placing it in Git, argv, logs, or chat.

Once the environment is exported, start Master and Worker as documented in `README.md`, then run `uv run agent-scheduler qualify`. Goal completion requires all 1/2/4/8-card evidence to pass; this document alone is not completion evidence.
