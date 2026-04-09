# Terminal Prompt Submission Design

**Date**: 2026-04-09
**Status**: Approved
**Branch**: `main`

## Problem

Task Ninja's interactive worker currently shows two related failures in the prompt-injection path:

1. **Copilot reaches its idle prompt in tmux but does not accept or submit the configured phase prompt**, leaving the ticket stuck in `planning`.
2. **Claude accepts the phase prompt but can echo junk characters before the slash command**, especially when multiple workers run in parallel.

The configured phase prompts are the same across profiles, so the divergence is in runtime behavior rather than profile data. The browser terminal wiring appears isolated per ticket, so the highest-value fix is in the backend worker/tmux automation path.

## Goals

- Prefer **one common injection strategy** across interactive CLIs.
- Keep the terminal visually clean when prompts are auto-injected.
- Prevent tickets from silently stalling in `planning`.
- Preserve worker isolation when multiple tickets run in parallel.
- Avoid frontend changes unless backend verification proves they are required.

## Approaches Considered

### 1. Universal verified submit (**Approved**)

Use one common handshake for all interactive CLIs:

`ready -> normalize -> submit -> verify -> retry/recover`

The default submit transport is literal text injection plus submit. True multiline transport stops being the default and is kept only as a fallback if a specific CLI later proves it is required.

**Why approved**

- Best matches the desired common strategy.
- Addresses both observed failures: Copilot's dropped submission and Claude's dirty echoed input.
- Makes "prompt was sent but not accepted" observable instead of silent.

### 2. Capability-based transport

Keep a shared orchestration flow but let each CLI choose its own transport (for example Claude on CSI-u, Copilot on literal submit).

**Why not chosen**

- Robust, but gives up on a truly common default path.
- Pushes complexity into per-CLI branching earlier than necessary.

### 3. Wrapper / non-interactive phase launch

Stop simulating keystrokes for automated phases and run them through wrappers or non-interactive CLI modes, keeping the terminal mainly for observation and manual follow-up.

**Why not chosen**

- Mechanically reliable, but changes Task Ninja's interaction model more than needed.
- Less aligned with the current live-terminal workflow.

## Approved Solution

### Runtime Handshake

Every interactive phase submission should go through a single backend handshake:

1. **Ready** — wait until the CLI is both visually idle and actually accepting keystrokes.
2. **Normalize** — clear transient input residue and ensure the cursor is positioned on the active input line.
3. **Submit** — inject the fully rendered phase prompt through one default transport.
4. **Verify** — confirm the CLI leaves the idle prompt after submission.
5. **Recover** — retry once, then escalate into explicit recovery instead of waiting forever.

### Default Transport

The default transport for all interactive CLIs becomes:

- literal text injection
- single submit action
- exact tmux target

Phase prompts may still be authored as multiline text in config, but the default runtime transport will normalize them into the transport format before injection.

True multiline CSI-u input is no longer the default path. It remains available as a future fallback if a CLI demonstrably requires it.

### Exact tmux Targeting

Automated injection should target the worker's **exact tmux pane id**, not a generic session name.

That keeps the automation anchored to one canonical target per worker and reduces ambiguity when grouped viewer sessions and monitor sessions exist at the same time.

## Component Changes

### `engine/worker.py`

`Worker` becomes the owner of the submission state machine.

It should consolidate the existing partial helpers into one authoritative phase-submission flow:

- startup readiness
- input probe
- prompt normalization
- submit
- verify
- retry / recover

Concrete behavior:

- Use the same handshake for the first phase and later phases.
- Stop treating submission as successful just because `send-keys` returned zero.
- Treat "still idle after submit" as a failed submission attempt.
- Keep structured internal state about attempt count and recovery reason.

### `engine/tmux.py`

`tmux` helpers should become thin exact-target primitives:

- send literal text
- send named key
- capture pane text
- read cursor position
- resolve pane id

The worker owns the policy. `tmux.py` only performs precise transport actions against the exact target.

### `api/routers/terminals.py` and `static/index.html`

No planned functional change.

The current evidence points to backend submission behavior rather than browser-side cross-routing. Frontend changes should only happen if later verification shows the backend fix is insufficient.

## Error Handling and Recovery

Task Ninja must never leave a ticket stuck in `planning` because a prompt was silently dropped.

Approved recovery policy:

1. Attempt normal submit.
2. If verification fails, run **one inline retry** through the same handshake.
3. If the second attempt still fails, record a specific submission error and move the ticket back to **`queued` once** so the orchestrator can retry from a fresh worker session.
4. If the ticket hits the same submission failure again on that rerun, mark it **`failed`**.

This gives one automatic self-heal for transient startup races while still stopping hard on persistent incompatibilities.

## Observability

Add structured worker logging for the handshake, including:

- readiness reached
- probe passed / failed
- submit attempt number
- verify passed / failed
- exact tmux target used
- recovery action taken

The logs should explain what happened without dumping full prompt contents into persistent logs by default.

## Testing

### Unit coverage

- tmux exact-target helper behavior
- prompt normalization logic
- submission verification logic

### Worker behavior coverage

- ready but dropped submission
- verify fails once then retry succeeds
- verify fails twice and triggers requeue
- repeated submission failure on rerun transitions to `failed`

### Parallel isolation coverage

- two workers maintain separate tmux pane targets
- one worker's submit / verify flow cannot mutate another worker's phase state

## Expected Outcome

After the change:

- Copilot should not remain idle after Task Ninja thinks planning started.
- Claude should stop showing stray prefixed junk before the slash command under the default path.
- Submission failures should be visible, retried deterministically, and never remain silent.
