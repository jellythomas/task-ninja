# Unified Ticket Modal with 3-Tier Assignment

**Date**: 2026-03-06
**Status**: Approved

## Problem

When loading tickets from an Epic or pasting ticket keys, users need to assign:
- Which **repository** each ticket belongs to
- Which **parent branch** to fork from
- Which **agent profile** (AI model) executes the task

Currently, the modal has a single global parent branch and a single agent dropdown. This breaks when an epic contains tickets from multiple Jira projects (e.g., MC-* and CKYC-* in the same epic).

## Design: 3-Tier Cascade

Resolution order: **Ticket override > Prefix group > Global default**

Each ticket inherits from its prefix group, which inherits from the global. Only set what you want to change.

### Tier 1: Global Defaults (always visible)

Top of the modal. Sets the fallback branch and agent for all tickets.

| Field | Default | Source |
|-------|---------|--------|
| Parent Branch | First matched repo's `default_branch` | Auto-detected |
| Agent Profile | Default profile (starred) | From settings |

### Tier 2: Prefix Group (auto-detected)

Tickets are grouped by their Jira key prefix (e.g., `MC`, `CKYC`). Each group gets a config bar:

| Field | Default | Source |
|-------|---------|--------|
| Repository | Auto-matched via `jira_label` | Prefix -> repo mapping |
| Parent Branch | Repo's `default_branch` | From matched repo |
| Agent Profile | Inherited from global | Can override |

- Groups auto-fill from the existing `jira_label -> repo` mapping
- Collapsed by default if values match auto-fill
- Click "Override" to expand and change
- Unmatched prefixes inherit from global defaults

### Tier 3: Per-Ticket Override (click to edit)

Click an edit icon on any ticket row to override its group's config:

| Field | Default | Source |
|-------|---------|--------|
| Repository | Inherited from group | Can override |
| Parent Branch | Inherited from group | Can override |
| Agent Profile | Inherited from group | Can override |

- Small colored dot/badge indicates customized tickets
- Override row appears inline below the ticket

## UI Mockup

```
+--------------------------------------------------+
| Select Tickets from MC-9056                      |
|                                                  |
| +- GLOBAL DEFAULTS ----------------------------+ |
| | Branch: main    Agent: Claude Code           | |
| +----------------------------------------------+ |
|                                                  |
| +- MC (5 tickets) --------------- [Override] --+ |
| |  Repo: mekari_credit  Branch: develop  CC    | |
| |  [ ] MC-9173  [BE] Flex PDAM Inquiry...      | |
| |  [ ] MC-9174  [BE] Flex PDAM Transaction  [e]| |
| |  [ ] MC-9175  [BE] PdamsController -- Op...  | |
| +----------------------------------------------+ |
|                                                  |
| +- CKYC (3 tickets) ------------- [Override] --+ |
| |  Repo: ckyc-service  Branch: main  Gemini    | |
| |  [ ] CKYC-401  Update KYC flow...            | |
| |  [ ] CKYC-402  Add validation...             | |
| +----------------------------------------------+ |
|                                                  |
|          Cancel              Queue Selected (8)  |
+--------------------------------------------------+
```

## Backend Changes

### Extend `AddTicketsRequest`

```python
class TicketAssignment(BaseModel):
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    profile_id: Optional[int] = None

class AddTicketsRequest(BaseModel):
    keys: list[str]
    summaries: Optional[dict[str, str]] = None
    # Legacy global fields (still supported as fallback)
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    profile_id: Optional[int] = None
    # Per-ticket overrides (takes precedence)
    assignments: Optional[dict[str, TicketAssignment]] = None
```

### Resolution in `add_tickets` endpoint

```python
for key in req.keys:
    # Per-ticket assignment > global
    assignment = (req.assignments or {}).get(key, TicketAssignment())
    repo_id = assignment.repository_id or req.repository_id
    branch = assignment.parent_branch or req.parent_branch
    profile = assignment.profile_id or req.profile_id
    # ... create ticket with resolved values
```

## Frontend Changes

### Data model

```javascript
// Computed from epicTickets
epicGroups: {
  'MC': {
    repoId: 1, branch: 'develop', profileId: 2,
    overridden: false,
    tickets: [{ key: 'MC-9173', ... }, ...]
  },
  'CKYC': {
    repoId: 3, branch: 'main', profileId: 5,
    overridden: true,
    tickets: [{ key: 'CKYC-401', ... }, ...]
  }
}

// Per-ticket overrides (sparse — only set for customized tickets)
ticketOverrides: {
  'MC-9174': { repoId: 2, branch: 'feature-x', profileId: 3 }
}
```

### Build assignments at queue time

```javascript
const assignments = {};
for (const ticket of selectedTickets) {
    const prefix = ticket.key.split('-')[0];
    const group = epicGroups[prefix] || {};
    const override = ticketOverrides[ticket.key] || {};
    assignments[ticket.key] = {
        repository_id: override.repoId || group.repoId || globalRepoId,
        parent_branch: override.branch || group.branch || globalBranch,
        profile_id: override.profileId || group.profileId || globalProfileId,
    };
}
```

## Implementation Steps

1. **Backend**: Add `TicketAssignment` model, extend `AddTicketsRequest` with `assignments` dict
2. **Backend**: Update `add_tickets` endpoint to resolve per-ticket assignments
3. **Frontend**: Group `epicTickets` by prefix, render collapsible group headers with config bars
4. **Frontend**: Add per-ticket edit icon with inline override row
5. **Frontend**: Build `assignments` dict at queue time by cascading 3 tiers
6. **Frontend**: Visual indicators for overridden groups/tickets
