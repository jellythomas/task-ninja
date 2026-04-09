# Copilot Prefix-Echo Prompt Submission Design

## Problem

The remaining Copilot regression is no longer the PTY teardown race. A disposable tmux probe showed that Copilot does accept `/planning-task MC-9384 parent:master` and starts `Thinking` after `Enter`. The failure is earlier in Task Ninja's pre-submit verification:

1. the worker normalizes the full phase prompt into one long line
2. the prompt includes the appended marker instruction
3. `_wait_for_prompt_echo()` currently waits for that *entire* normalized string to appear as one visible composer line
4. Copilot's composer does not render that full long string contiguously, so the worker concludes the prompt was never accepted and never reaches the successful submit path

This produces false deterministic prompt-submission failures even though the command head is visible and Copilot would accept the prompt if `Enter` were sent.

## Chosen Approach

Use a **prefix-based echo proof plus post-Enter pane delta**:

1. Keep exact-pane targeting and literal transport as the default send path.
2. Derive a short **visible echo prefix** from the injected prompt.
   - For slash-command prompts, this is the command head and visible arguments (for example `/planning-task MC-9384 parent:master`).
   - It deliberately excludes the appended marker tail that may wrap or be visually truncated.
3. Wait for that prefix to appear in the composer instead of waiting for the full normalized prompt.
4. Press `Enter`.
5. Keep the existing post-Enter pane-delta verification:
   - success if the composer line leaves its pre-submit state, or
   - success if fresh non-composer output appears (`Thinking`, transcript growth, etc.)

## Components

### `engine/worker.py`

- Add a helper that derives the visible echo prefix from the normalized prompt.
- Update `_find_composed_input_line()` / `_wait_for_prompt_echo()` to match on that prefix instead of the full prompt text.
- Keep `_verify_prompt_submitted()` focused on post-Enter pane transitions, not footer heuristics.
- Preserve the current deterministic failure path and persisted retry policy; only the pre-submit proof changes.

### Tests

- Extend `tests/test_worker_prompt_submission.py` to cover long prompts where:
  - the full normalized string is not visible contiguously
  - the slash-command head is visible
  - the pane transitions to fresh output after `Enter`
- Keep existing PTY race and deterministic retry-policy tests in the focused validation set.

## Trade-offs

- This keeps the common backend strategy across CLIs instead of introducing Copilot-specific submit adapters.
- The echo-prefix helper adds a little prompt parsing logic, but it matches what Copilot visibly renders and therefore avoids the false-negative caused by wrapped or truncated long prompts.
- The post-Enter pane-delta check remains the authoritative proof of acceptance, so shortening the echo probe does not weaken final verification.
