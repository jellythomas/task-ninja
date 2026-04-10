# Prompt Verification + PTY Teardown Regression Design

## Problem

The current prompt-submission regression has two linked symptoms:

1. `Worker._verify_prompt_submitted()` can report "still at prompt" even after a CLI accepted input, because it relies on persistent footer phrases such as `bypass permissions` and `type @ to mention`.
2. When that false-negative path tears down the worker, `_pty_read_loop()` can race with `_close_pty()` / `_reconnect_monitor_pty()` and attempt `select.select([self._master_fd], ...)` after `self._master_fd` has already become `None`, producing a traceback instead of a clean exit.

The fix should preserve the recently approved common tmux submission strategy while making submission verification authoritative and teardown race-safe.

## Chosen Approach

Use a **universal pane-delta handshake** for prompt submission:

1. Keep exact-pane targeting and the common literal transport as the default path.
2. Stop using idle footer phrases as the proof that submission succeeded or failed.
3. Verify submission from the prompt's own pane-state transition:
   - capture the relevant visible pane state before typing
   - send the normalized prompt to the exact pane target
   - confirm that the prompt text appears in the composer area
   - press Enter
   - confirm that the pane moves away from that exact composed input line or starts emitting fresh non-composer output
4. Retain the existing inline retry and persisted requeue policy, but only when the pane-delta handshake cannot prove submission.

For PTY teardown, make fd lifecycle explicit:

1. Snapshot the current PTY fd before each executor-backed `select` / `read`.
2. Exit the read loop cleanly if the fd is `None`, changed, or already closed by teardown or reconnect.
3. Treat worker shutdown and monitor reconnect as normal loop termination paths, not exceptional errors worth logging as tracebacks.

## Components

### `engine/worker.py`

- Refactor `_submit_phase_prompt()` to use pane-delta verification instead of `_is_cli_at_prompt()` as the authoritative submission gate.
- Add small helpers as needed for:
  - capturing the visible pane around the input line
  - detecting whether the submitted prompt text appeared
  - detecting whether Enter changed the composer/output state
- Keep `_is_cli_at_prompt()` only as a startup/readiness heuristic.
- Harden `_pty_read_loop()` so executor work never dereferences a mutable `self._master_fd` after teardown changed it.

### `engine/tmux.py`

- Reuse existing exact-target helpers (`send_literal_text`, `send_key`, `capture_pane`) without changing the transport model.
- Only add helper support if the worker needs a narrower pane capture or cursor-oriented primitive for the pane-delta check.

### Tests

- Extend submission tests to prove footer phrases alone do not count as failed submission when pane content shows the prompt was typed and accepted.
- Add a PTY read-loop regression test that simulates `_master_fd` becoming `None` / closed during executor-backed reads and asserts a clean exit.
- Keep current retry-policy tests so first deterministic submission failure still requeues once and repeat failure still becomes `FAILED`.

## Trade-offs

- This keeps one common strategy across CLIs, which matches the approved direction and avoids per-CLI submit code paths.
- The pane-delta verifier is slightly more complex than footer heuristics, but it is grounded in the actual prompt we injected and therefore less likely to misclassify active sessions.
- PTY lifecycle guards add a little branching in the read loop, but they turn shutdown races into intentional control flow instead of noisy tracebacks.
