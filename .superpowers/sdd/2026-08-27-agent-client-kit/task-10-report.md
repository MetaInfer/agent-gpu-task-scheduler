# Task 10 Report: Internal Qualification Docs and Final Gates

Date: 2026-08-27
Worktree: `/public/share/fh/agent-gpu-task-scheduler/.claude/worktrees/agent-a9db0c36df0da3eb1`

## Seed and baseline

Seeded the reviewed coordinator commits in the requested order:

```text
5473aac 626f752 dc4c711 65ee0e9 f1eceed 73b2d12 ccc0f10 a961cec
d2d46e1 f5af213 b14d17f ac1b714 a6e81b2 6cea9ee
```

Command:

```bash
git cherry-pick 5473aac 626f752 dc4c711 65ee0e9 f1eceed 73b2d12 ccc0f10 a961cec d2d46e1 f5af213 b14d17f ac1b714 a6e81b2 6cea9ee
```

Output: all 14 commits cherry-picked successfully. The final seeded local commit is `2097802` (`test: harden client wheel isolation gates`).

Commands and output:

```bash
git rev-parse HEAD^{tree}
# 181af8fe66c06901daed8c0e5e3db0d1e43f4e4b

git rev-parse 6cea9ee^{tree}
# 181af8fe66c06901daed8c0e5e3db0d1e43f4e4b

git status --short --branch
# ## worktree-agent-a9db0c36df0da3eb1
```

The seeded tree exactly matched `6cea9ee^{tree}` and was clean before Task 10 edits.

## RED

Added the exact requested assertion `test_internal_submitter_test_doc_describes_source_isolation` to `tests/test_client_docs.py` before changing living documentation.

Command:

```bash
python3 -m pytest tests/test_client_docs.py -k internal_submitter -v
```

Output (exit 1):

```text
collected 8 items / 7 deselected / 1 selected
tests/test_client_docs.py F
E       AssertionError: assert 'agent-scheduler-submitter' in text
FAILED tests/test_client_docs.py::test_internal_submitter_test_doc_describes_source_isolation
1 failed, 7 deselected, 1 warning in 0.43s
```

The assertion failed for the intended reason: the internal guide still described the old source-root workflow and did not name the installed client entrypoint.

## GREEN

Updated the living documentation to state:

- T1 exercises `agent_scheduler_client` and the installed `agent-scheduler-submitter` entrypoint directly.
- T2/T3 create a per-run client workspace, copy only the canonical Submitter skill plus generated connection configuration, and launch the Agent there.
- T2/T3 do not mount the server repository or use it as Agent cwd.
- Internal provider documentation retains Master/Worker prerequisites, all explicit real-test flags, and cost/GPU warnings.
- Client/public and Provider/internal documentation paths are distinguished.
- Ruff and mypy are separate direct `python3` commands, and mypy checks `src packages/client/src`.

Command:

```bash
python3 -m pytest tests/test_client_docs.py -k internal_submitter -v
```

Output (exit 0):

```text
collected 8 items / 7 deselected / 1 selected
tests/test_client_docs.py .
1 passed, 7 deselected, 1 warning in 0.16s
```

## Full default suite after documentation changes

All real opt-ins were removed from the child shell before collection.

Command:

```bash
unset RUN_REAL_CLAUDE RUN_REAL_CODEX RUN_REAL_PI RUN_REAL_DSH RUN_REAL_GPU RUN_FULL_QUALIFICATION; python3 -m pytest
```

Output (exit 0):

```text
209 passed, 16 skipped, 1 warning in 17.45s
```

The warning was the existing `StarletteDeprecationWarning` from `fastapi.testclient` concerning `httpx`/`httpx2`.

## Final zero-cost and static gates

The isolated command runner rejected the brief's `env -u ... python3 -m pytest` wrapper before process execution because it could not prove the wrapped `python3 -m` stayed in the worktree. I ran the shell-equivalent command with all six variables unset in the same child shell; no billed or GPU marker was selected.

Command:

```bash
unset RUN_REAL_CLAUDE RUN_REAL_CODEX RUN_REAL_PI RUN_REAL_DSH RUN_REAL_GPU RUN_FULL_QUALIFICATION; python3 -m pytest -m 'not real_claude and not real_codex and not real_pi and not real_dsh and not real_gpu'
```

Output (exit 0):

```text
209 passed, 6 skipped, 10 deselected, 1 warning in 12.73s
```

Commands and output:

```bash
python3 -m ruff check .
# All checks passed!

python3 -m mypy src packages/client/src
# Success: no issues found in 39 source files

git diff --check
# exit 0; no output
```

## Reused Task 9 artifacts

No artifact was rebuilt and no external package was installed, downloaded, or executed.

Presence checks:

```bash
ls -ld /tmp/agent-client-dist-task9-a8f06b28
find /tmp/agent-client-dist-task9-a8f06b28 -maxdepth 1 -type f -name '*.whl' -print
```

Output:

```text
drwxr-xr-x ... /tmp/agent-client-dist-task9-a8f06b28
/tmp/agent-client-dist-task9-a8f06b28/agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl
```

Wheel hash command and output:

```bash
sha256sum /tmp/agent-client-dist-task9-a8f06b28/agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl
# b2bd73b7f24c1a918ad71bee2114af9a63dc959f474a5859c98eac621063e6ad  /tmp/agent-client-dist-task9-a8f06b28/agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl
```

The wheel SHA-256 exactly matches the reported Task 9 hash.

Kit presence and entry-count commands:

```bash
ls -ld /tmp/agent-client-kit-0.2.0-task9-a8f06b28 /tmp/agent-client-kit-0.2.0-task9-a8f06b28/SHA256SUMS
wc -l /tmp/agent-client-kit-0.2.0-task9-a8f06b28/SHA256SUMS
```

Output:

```text
drwxr-xr-x ... /tmp/agent-client-kit-0.2.0-task9-a8f06b28
-rw-r--r-- ... /tmp/agent-client-kit-0.2.0-task9-a8f06b28/SHA256SUMS
16 /tmp/agent-client-kit-0.2.0-task9-a8f06b28/SHA256SUMS
```

Offline wheel gates:

```bash
AGENT_SCHEDULER_CLIENT_WHEEL=/tmp/agent-client-dist-task9-a8f06b28/agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl \
  python3 -m pytest tests/test_client_package.py tests/test_client_isolation.py -v
```

Output (exit 0):

```text
collected 9 items
tests/test_client_package.py ...... [ 66%]
tests/test_client_isolation.py ...  [100%]
9 passed, 1 warning in 13.81s
```

Kit checksum command:

```bash
(cd /tmp/agent-client-kit-0.2.0-task9-a8f06b28 && sha256sum -c SHA256SUMS)
```

Output (all 16 entries verified):

```text
MANIFEST.json: OK
config/codex-mcp.example.toml: OK
config/dsh-mcp.example.patch.yml: OK
config/mcp.example.json: OK
docs/submitting-from-an-agent-client.md: OK
skills/submit-gpu-task/SKILL.md: OK
skills/submit-gpu-task/reference/proposal-template.md: OK
wheels/agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl: OK
wheels/anyio-4.14.2-py3-none-any.whl: OK
wheels/certifi-2026.7.22-py3-none-any.whl: OK
wheels/exceptiongroup-1.3.1-py3-none-any.whl: OK
wheels/h11-0.16.0-py3-none-any.whl: OK
wheels/httpcore-1.0.9-py3-none-any.whl: OK
wheels/httpx-0.28.1-py3-none-any.whl: OK
wheels/idna-3.19-py3-none-any.whl: OK
wheels/typing_extensions-4.16.0-py3-none-any.whl: OK
```

Kit source-isolation command:

```bash
python3 - <<'PY'
from pathlib import Path
root = Path('/tmp/agent-client-kit-0.2.0-task9-a8f06b28')
for path in root.rglob('*'):
    assert not path.is_symlink(), path
    assert 'agent_scheduler/' not in path.as_posix(), path
print('Client Kit source-isolation checks passed')
PY
```

Output (exit 0):

```text
Client Kit source-isolation checks passed
```

## Authorization-dependent validation explicitly skipped

T2 billed Agent onboarding was not authorized and was not run:

```bash
RUN_REAL_CLAUDE=1 python3 -m pytest tests/test_real_onboarding.py -m real_claude -v
RUN_REAL_CODEX=1  python3 -m pytest tests/test_real_onboarding.py -m real_codex -v
RUN_REAL_PI=1     python3 -m pytest tests/test_real_onboarding.py -m real_pi -v
RUN_REAL_DSH=1    python3 -m pytest tests/test_real_onboarding.py -m real_dsh -v
```

T3 billed Agent plus real-GPU qualification was not authorized and was not run:

```bash
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CLAUDE=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[claude-RUN_REAL_CLAUDE]' -v
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CODEX=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[codex-RUN_REAL_CODEX]' -v
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_PI=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[pi-RUN_REAL_PI]' -v
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_DSH=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[dsh-RUN_REAL_DSH]' -v
```

These commands are skipped authorization-dependent validation, not passing evidence.

## Files and self-review

Task 10 files:

- `README.md`
- `docs/testing-the-submitter.md`
- `docs/usage.md`
- `tests/test_client_docs.py`
- `.superpowers/sdd/2026-08-27-agent-client-kit/task-10-report.md`

Self-review findings:

- The test is the exact assertion required by the Task 10 brief and was observed RED before documentation edits and GREEN afterward.
- The internal guide still contains Provider-side fake/Claude Master modes, Worker requirements, credentials, harness prerequisites, cost warnings, and all explicit T2/T3 opt-ins.
- README and usage now clearly separate Client/public guidance from Provider/internal guidance.
- All changed living documentation uses direct `python3` commands; no `uv` command was introduced.
- No Task 1-9 implementation defect was exposed, so no source file or `tests/test_qualification.py` change was needed.
- Historical files under `docs/superpowers/` were not changed.
- The diff is limited to the requested documentation assertion, living docs, and this report.

Concerns:

- T2/T3 remain unverified in this task because they require separate authorization for billed Agent and GPU use.
- The test suite emits one pre-existing `StarletteDeprecationWarning` about `httpx`/`httpx2`; it does not fail any gate.
- The command runner prevented the literal `env -u` wrapper, so the recorded zero-cost execution used equivalent shell `unset` semantics.
- No external artifact gap remains: both exact Task 9 paths were present and all requested hash/isolation gates passed.
