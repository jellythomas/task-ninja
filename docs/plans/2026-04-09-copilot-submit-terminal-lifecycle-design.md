# Copilot Submit Verification and Terminal Lifecycle Design

## Problem

Task Ninja still leaves some Copilot planning tickets apparently idle after retry. The current live symptom is twofold:

1. The worker/ticket can remain in `planning` without the old deterministic prompt-submission error.
2. The actual Copilot pane and the web terminal can both settle back to the idle prompt, and the browser terminal may later close with WebSocket code `1006`.

This means the remaining issue is not only transport. The submit verifier can still produce false positives, and the interactive terminal viewer can interfere with or lose the live session during worker startup.

## Root Causes

### 1. False-positive submit verification

`engine/worker.py::_verify_prompt_submitted()` currently accepts a prompt when the composed input line disappears after Enter. That was safe when the full composer echo was visible, but it is no longer sufficient after switching to a shorter visible prefix. Copilot can repaint back to its normal idle prompt without ever starting real work, causing the visible prefix to disappear and the worker to classify the submission as accepted.

### 2. Viewer attach injects input during worker startup

The frontend auto-opens the fullscreen interactive terminal as soon as a ticket enters `planning` or `developing`. When the xterm WebSocket opens, it immediately sends a resize message and then a delayed `Ctrl+L` redraw. Because this input reaches the same grouped tmux session as the worker, simply opening the live terminal can perturb the auto-submit handshake and create redraw noise during verification.

### 3. Viewer PTY failures degrade into abnormal browser closes

Grouped tmux viewer sessions are detached through per-viewer PTYs. If a grouped session or its PTY dies, the viewer read loop can end without a clean app-level close path, which leaves the browser terminal showing an abnormal `1006` close instead of a deterministic reason and reconnect behavior.

## Chosen Approach

Use a two-part hardening strategy:

1. **Stricter submit verification** that requires positive post-Enter evidence instead of treating composer disappearance as success.
2. **Viewer lifecycle cleanup** that keeps the terminal visible and interactive without letting viewer startup interfere with worker-owned prompt submission.

This preserves the existing tmux architecture and the common cross-CLI submission strategy while fixing the Copilot-specific false-positive case and the broken live-terminal UX.

## Design

### A. Worker-owned positive submit verification

Keep the exact-pane tmux transport unchanged:

- send literal prompt to the exact pane
- confirm visible pre-submit composer echo using the short visible prefix
- send Enter from the worker

Change post-Enter verification to classify pane transitions into three categories:

1. **Real progress**
   - busy/working indicators
   - command-dispatch UI
   - fresh non-idle output that is not just a redraw
   - eventual phase marker
2. **Idle redraw**
   - pane re-renders back to the normal idle prompt
   - visible prefix disappears, but no progress signal follows
3. **Stuck composer**
   - composed line remains visible and no progress occurs

Only **real progress** counts as accepted submission. Both **idle redraw** and **stuck composer** are failures for that attempt and continue through the existing retry/requeue policy. This removes the current false positive where Copilot repaints to idle and Task Ninja assumes Enter worked.

### B. Submission-safe terminal attach

Introduce a transient worker-owned startup guard for the phase-prompt submission window. While this guard is active:

- viewers may attach
- viewers may resize their PTYs
- viewers must not inject redraw/control input that affects the shared CLI pane

In practice, the fullscreen xterm can still auto-open, but its delayed `Ctrl+L` redraw and similar attach-time control input are deferred until the worker finishes the prompt-submission handshake. This preserves the current UX of automatically showing the live terminal without allowing the viewer to perturb the worker at the exact moment the worker is typing and submitting the phase prompt.

### C. Clean viewer failure and reconnect path

Keep grouped tmux sessions, but make viewer failure explicit:

- when a grouped viewer PTY/read loop dies, close the WebSocket with an app-level reason instead of letting the browser observe a generic abnormal close
- ensure grouped viewer sessions are cleaned up exactly once
- allow the frontend to reconnect the active terminal when the worker is still running

This keeps one active viewer stream per open terminal tab and eliminates the current situation where the browser is left with a dead terminal and only a `1006` close.

## Error Handling

- Do not change the existing deterministic prompt-submission retry policy.
- If no positive post-Enter submit signal appears, treat the attempt as a genuine submission failure.
- If the viewer stream dies while the worker is still alive, surface that as a terminal-stream error, not as prompt-submission success or failure.

## Testing

### Worker tests

- idle redraw after Enter must **not** count as accepted submission
- true Copilot progress/busy transitions must count as accepted submission
- stuck composer must still fail deterministically

### Viewer/terminal tests

- attach-time viewer setup must not send redraw input during the guarded startup window
- grouped viewer PTY death must trigger deterministic cleanup and close behavior
- reconnecting an active terminal must not create duplicate live streams for the same tab

### Live regression

Re-run the `MC-9384`-style retry path and confirm:

- Copilot leaves the idle prompt and actually starts planning
- the ticket does not bounce back to fake success/idle
- the web terminal stays connected and does not show `1006`
