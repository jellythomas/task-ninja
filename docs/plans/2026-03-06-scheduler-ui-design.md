# Scheduler Settings UI

**Date**: 2026-03-06
**Status**: Approved

## Overview

Add a "Scheduler" tab in Settings that combines schedule management, auto-retry, and working hours into one configuration page. Schedules work as templates that re-run existing board tickets (already queued/configured) on a cron or one-time basis.

## Design

### Section 1: Schedules

Schedules are templates that trigger runs for tickets already on the board. They don't create new tickets — they re-execute what's already configured.

**List View:**
- Each schedule shows: name/description, type (recurring/one-time), cron expression (human-readable), next run time, enabled toggle, delete button

**Create Form:**
- Type: Recurring or One-time
- For Recurring: Visual cron builder + raw cron input
- For One-time: Datetime picker
- Optional end time for recurring schedules

**Visual Cron Builder:**
- Preset buttons: "Every weekday 9AM", "Every hour", "Every 6h", "Every night midnight"
- Day-of-week toggles: Mon-Sun checkboxes
- Hour picker: dropdown or slider
- Minute picker: dropdown
- Live preview: "Runs every weekday at 09:00" + raw cron string

### Section 2: Auto-Retry

Toggle + numeric inputs, saves to .env:

| Field | Key | Default |
|-------|-----|---------|
| Enabled toggle | AUTO_RETRY_ENABLED | false |
| Delay (minutes) | AUTO_RETRY_DELAY_MINUTES | 15 |
| Max retries | AUTO_RETRY_MAX | 3 |

### Section 3: Working Hours

Toggle + time/day inputs, saves to .env:

| Field | Key | Default |
|-------|-----|---------|
| Enabled toggle | WORKING_HOURS_ENABLED | false |
| Start time | WORKING_HOURS_START | 09:00 |
| End time | WORKING_HOURS_END | 18:00 |
| Days | WORKING_HOURS_DAYS | mon,tue,wed,thu,fri |

## Backend Changes

1. **PATCH `/api/schedules/{schedule_id}`** — toggle enabled, update cron_expression
2. Auto-retry and working hours already work via `POST /api/settings/env`

## Implementation Steps

1. Add PATCH endpoint for schedules
2. Add "Scheduler" to settings tabs
3. Build schedules list + create form
4. Build visual cron builder
5. Build auto-retry section
6. Build working hours section
