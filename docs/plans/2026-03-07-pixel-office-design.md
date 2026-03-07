# Pixel Office View — Design Document

**Date:** 2026-03-07
**Status:** Approved
**Approach:** Hybrid (Canvas 2D rendering + DOM overlays)

---

## Overview

A toggleable alternative view for the Task Ninja board that visualizes AI agent workers as animated pixel art characters in a virtual office. Default view remains Kanban; users can switch to Pixel Office via a header toggle. Same data, same SSE feed, different visualization.

**Inspiration:** [Pixel Agents](https://github.com/pablodelucca/pixel-agents) — a VS Code extension that turns Claude Code agents into animated pixel characters.

---

## Architecture

### Hybrid Rendering (Approach C)

```
┌──────────────────────────────────────┐
│         Alpine.js Data Layer         │
│  (tickets, states — same as kanban)  │
└──────────────┬───────────────────────┘
               │ reactive data
               ▼
┌──────────────────────────────────────┐
│          DOM Layer (Alpine.js)        │
│  ┌──────────┐  ┌──────────────────┐  │
│  │ Toolbar   │  │ Popup Card       │  │
│  │ (view     │  │ (ticket info,    │  │
│  │  toggle,  │  │  terminal btn,   │  │
│  │  theme,   │  │  pause/resume)   │  │
│  │  sound)   │  │                  │  │
│  └──────────┘  └──────────────────┘  │
├──────────────────────────────────────┤
│        Canvas Layer (game loop)      │
│  ┌──────────────────────────────┐    │
│  │ Office: tiles, desks, chars, │    │
│  │ walking, speech bubbles,     │    │
│  │ animations                   │    │
│  └──────────────────────────────┘    │
└──────────────────────────────────────┘
```

- **Canvas** renders the pixel-art office scene (tiles, furniture, characters, animations)
- **DOM** handles UI overlays (toolbar, popup cards, tooltips) styled with Tailwind
- **Alpine.js** manages all state; Canvas reads from it each frame
- **SSE** updates Alpine data → Canvas reflects changes next frame
- **No new server endpoints** — uses existing ticket state data

### Why Hybrid

| Layer | Responsibility | Why |
|-------|---------------|-----|
| Canvas 2D | Office scene rendering | Pixel-perfect, `imageSmoothingEnabled=false`, game-loop animations |
| DOM/Alpine | Popup cards, toolbar, sound toggle | Native click/hover, Tailwind styling, matches existing kanban card design |

---

## Technical Specs

Reference values from Pixel Agents, adapted for our needs:

| Setting | Value | Notes |
|---------|-------|-------|
| Tile size | 16×16 px | Grid unit for floor, walls, furniture |
| Character sprite | 16×24 px | Taller than a tile (head + body) |
| Game loop | `requestAnimationFrame` | ~60fps render, delta-time based |
| Walk animation FPS | ~6.7 (0.15s/frame) | 2-frame walk cycle |
| Type animation FPS | ~3.3 (0.3s/frame) | 2-frame typing cycle |
| Walk speed | 48 px/sec (~3 tiles/sec) | Smooth tile-to-tile movement |
| Default office | Auto-sized based on ticket count | Min 12×8, grows with tickets |
| Zoom | Integer levels (1×–4×) | CSS `image-rendering: pixelated` |
| Image smoothing | `false` | Crisp pixel scaling |

---

## Sprite System

### Code-Defined Pixel Art (Option D)

All sprites are defined as JavaScript 2D arrays of color indices. No external image files.

```javascript
// Example: 16×24 character defined as color index array
const PALETTE = ['transparent', '#2a1f3d', '#f5d6b8', '#4a90d9', ...];
const CHAR_IDLE_0 = [
  [0,0,0,0,0,1,1,1,1,0,0,0,0,0,0,0],
  [0,0,0,0,1,2,2,2,2,1,0,0,0,0,0,0],
  // ... 22 more rows
];
```

At startup, these arrays are rendered to off-screen canvases → cached as sprite sheets for fast `drawImage()` calls.

### Character Animation Frames

Minimal drawn set, enhanced with code transforms:

| Frame | Drawn? | Code Enhancement |
|-------|--------|-----------------|
| Idle frame 0 | Yes | — |
| Idle frame 1 | Yes | Subtle Y-shift for breathing |
| Walk right frame 0 | Yes | Mirror horizontally for walk-left |
| Walk right frame 1 | Yes | Mirror horizontally for walk-left |
| Sit + type frame 0 | Yes | — |
| Sit + type frame 1 | Yes | — |
| Sit + read frame 0 | Yes | — |
| Sit + read frame 1 | Yes | — |
| Error state | No | Idle + red tint overlay + "!" bubble |
| Celebrate state | No | Idle + Y-bounce + "✓" bubble |

**Total: ~8 drawn frames per character base.** Walk-left derived by mirroring.

### Palette Swap for Character Variants

One base character → 4–6 color variants by swapping palette entries:

```javascript
const VARIANTS = [
  { hair: '#3a2a1a', shirt: '#4a90d9', pants: '#2d3748' }, // Variant 1
  { hair: '#c0392b', shirt: '#27ae60', pants: '#34495e' }, // Variant 2
  { hair: '#f39c12', shirt: '#8e44ad', pants: '#2c3e50' }, // Variant 3
  // ...
];
```

Swap at sprite-sheet generation time → zero runtime cost.

### Theme Packs

| Pack | Description | Default |
|------|-------------|---------|
| **Office Workers** | Diverse workers with different hair/shirt/pants colors | Yes |
| **Ninja** | Masked ninjas with colored outfits and headbands | No |

Both packs share the same frame structure. Theme selectable from pixel office toolbar. Saved to localStorage.

### Environment Tiles (Code-Defined)

| Tile | Size | Animated |
|------|------|----------|
| Floor (carpet/wood) | 16×16 | No |
| Wall (top border) | 16×16 | No |
| Desk with monitor | 32×16 (2 tiles wide) | Monitor glow (2 frames) |
| Chair | 16×16 | No |
| Door/entrance | 16×32 (1 tile wide, 2 tall) | No |

---

## Office Layout (Auto-Generated)

The office auto-arranges based on active ticket count. No editor needed.

### Layout Algorithm

```
┌─────────────────────────────────────────┐
│ WALL  WALL  WALL  WALL  WALL  WALL  WALL│
│                                         │
│ DOOR   [LOBBY AREA]                     │
│                                         │
│        ┌─────┐  ┌─────┐  ┌─────┐       │
│        │Desk1│  │Desk2│  │Desk3│       │
│        │Chair│  │Chair│  │Chair│       │
│        └─────┘  └─────┘  └─────┘       │
│                                         │
│        ┌─────┐  ┌─────┐  ┌─────┐       │
│        │Desk4│  │Desk5│  │Desk6│       │
│        │Chair│  │Chair│  │Chair│       │
│        └─────┘  └─────┘  └─────┘       │
│                                         │
│              [EXIT AREA]          EXIT   │
└─────────────────────────────────────────┘
```

- **Desk count** = number of unique tickets on the board (or `max_parallel` workers, whichever is larger)
- **Desks arranged** in rows of 3, adding rows as needed
- **Office auto-sizes** to fit: min 12×8 tiles, max 24×16 tiles
- **Three zones:**
  - **Lobby** (near door) — queued characters stand here
  - **Desks** (center) — active characters sit here
  - **Exit** (bottom/right) — completed characters walk here

### Desk Assignment

- Each ticket gets a desk assigned when it enters PLANNING
- Desk is freed when ticket reaches DONE/FAILED (after exit animation)
- Desks are reused in order (desk 1 first, then 2, etc.)

---

## Character State Machine

### Ticket State → Character Behavior

```
                    ┌──────────┐
   Ticket created → │  LOBBY   │ (idle, standing near door)
                    └────┬─────┘
                         │ QUEUED → PLANNING
                         │ (walks to assigned desk)
                         ▼
                    ┌──────────┐
                    │  DESK    │ (sit + read animation)
                    └────┬─────┘
                         │ PLANNING → DEVELOPING
                         ▼
                    ┌──────────┐
                    │  DESK    │ (sit + type animation)
                    └────┬─────┘
                         │ DEVELOPING → REVIEW/DONE
                         │ (stands up, walks to exit)
                         ▼
                    ┌──────────┐
                    │  EXIT    │ (celebrate + walk out)
                    └──────────┘

    FAILED: stays at desk, red tint + "!" bubble
    RETRY:  stands up, walks to lobby, then re-enters
```

### State Machine Per Character

```
SPAWN → IDLE_LOBBY → WALK_TO_DESK → SIT_READ → SIT_TYPE → STAND_UP → WALK_TO_EXIT → CELEBRATE → DESPAWN
                                                    │
                                                    ├── FAILED (red tint, stays seated)
                                                    │
                                                    └── RETRY → STAND_UP → WALK_TO_LOBBY → IDLE_LOBBY
```

### Movement: BFS Pathfinding

- Office is a 2D tile grid with walkable/blocked flags
- Desks and walls are blocked tiles
- BFS finds shortest path from current tile to target tile
- Character walks tile-by-tile at 48px/sec (smooth sub-pixel interpolation)
- Direction changes update sprite to face walk direction

---

## Character Interaction

### Click → Popup Card

When user clicks on a character in the canvas:

1. Canvas click handler translates mouse coordinates to tile/character
2. If a character is hit, Alpine.js state is updated: `pixelOffice.selectedCharacter = ticketId`
3. DOM popup card appears positioned near the character (converted from canvas coords to screen coords)

### Popup Card Content (matches kanban card)

- Ticket key + summary (e.g., "MC-9173 — Fix login bug")
- State badge (colored pill: Planning, Developing, etc.)
- Elapsed time since state entered
- Repository + branch info
- **Action buttons:**
  - "Open Terminal" → opens Live Process overlay
  - "Pause / Resume" (for active tickets)
  - "Delete" (with confirmation)
- Click outside → dismiss

---

## Sound System

### Web Audio API (No Audio Files)

All sounds are generated programmatically using the Web Audio API — zero external dependencies.

| Event | Sound Type | Trigger |
|-------|-----------|---------|
| Ticket starts working | Soft ascending chime | QUEUED → PLANNING |
| Typing ambient | Quiet keyboard clicks (loop) | While DEVELOPING |
| Ticket completed | Success jingle (3 ascending notes) | → DONE |
| Ticket failed | Low error buzz | → FAILED |
| Background ambient | Subtle low hum (optional) | While office is active |

### Sound Controls

- **Master toggle:** Sound on/off (default: off)
- **Volume slider:** 0–100%
- Saved to localStorage
- Located in pixel office toolbar

### Implementation

```javascript
// Example: generate a chime sound
function playChime() {
  const ctx = new AudioContext();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.frequency.setValueAtTime(523, ctx.currentTime);      // C5
  osc.frequency.setValueAtTime(659, ctx.currentTime + 0.1); // E5
  osc.frequency.setValueAtTime(784, ctx.currentTime + 0.2); // G5
  gain.gain.setValueAtTime(0.3, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5);
  osc.start();
  osc.stop(ctx.currentTime + 0.5);
}
```

---

## Speech Bubbles

Rendered on the Canvas layer, floating above character heads.

| Ticket State | Bubble Content |
|-------------|---------------|
| QUEUED | `⏳ MC-9173` |
| PLANNING | `📖 MC-9173` |
| DEVELOPING | `⌨️ MC-9173` |
| REVIEW | `📋 MC-9173` |
| DONE | `✓ MC-9173` |
| FAILED | `! MC-9173` (red) |

- Bubbles have a pixel-art rounded rectangle background
- Fade in/out over 0.5s on state transitions
- Show ticket key (truncated if needed)

---

## UI Components

### 1. View Toggle (Header)

Located in the board header next to existing controls:

```
[📋 Kanban] [🎮 Pixel Office]
```

- Toggle button group, active state highlighted
- View preference saved to localStorage
- Both views share the same Alpine.js ticket data

### 2. Pixel Office Toolbar

Floating toolbar at top of the pixel office view:

```
[Theme: Office Workers ▼] [🔊 Sound: Off] [Volume: ███░░]
```

- Theme selector dropdown (Office Workers / Ninja)
- Sound toggle
- Volume slider (visible when sound is on)

### 3. Canvas Container

```html
<div x-show="boardView === 'pixel'" class="relative w-full h-full">
  <canvas id="pixelOfficeCanvas" class="w-full h-full" style="image-rendering: pixelated;"></canvas>
  <!-- DOM overlays positioned absolutely -->
  <div x-show="pixelOffice.selectedCharacter" class="absolute ..." :style="popupPosition">
    <!-- Popup card -->
  </div>
</div>
```

---

## Data Flow

```
SSE Event (ticket state change)
    │
    ▼
Alpine.js data update (this.tickets[id].state = 'DEVELOPING')
    │
    ├──► Kanban view re-renders (if active) — existing behavior
    │
    └──► Pixel Office engine reads new state
            │
            ├──► Character state machine transitions
            │     (e.g., IDLE_LOBBY → WALK_TO_DESK → SIT_TYPE)
            │
            ├──► Sound trigger (if enabled)
            │     (e.g., playChime() on QUEUED → PLANNING)
            │
            └──► Canvas renders next frame with updated characters
```

No new API endpoints. No new SSE events. The pixel office is purely a different renderer for the same data.

---

## Performance Considerations

| Concern | Mitigation |
|---------|-----------|
| Canvas redraw every frame | Only redraw dirty regions (characters that moved). Static tiles cached to off-screen canvas |
| Many characters | Practical limit ~20 characters. Beyond that, office gets crowded anyway |
| Sprite generation | One-time cost at startup. Cache sprite sheets as off-screen canvases |
| Memory | ~8 frames × 16×24px × 6 variants = ~18KB of pixel data. Negligible |
| Game loop when not visible | Pause game loop when kanban view is active or tab is hidden |
| Sound | Web Audio API is lightweight. Sounds are <0.5s each |
| Mobile | Canvas scales to container. Touch events mapped same as click |

---

## File Changes

All changes contained within `static/index.html`:

| Section | What's Added |
|---------|-------------|
| HTML | `<canvas>` element, popup card overlay, toolbar |
| Alpine.js data | `boardView`, `pixelOffice` state object, theme/sound settings |
| JavaScript | Pixel Office engine: game loop, sprite system, pathfinding, state machine, sound manager |
| CSS | Canvas container, popup positioning, toolbar styling |

Estimated addition: ~800–1200 lines of JavaScript for the engine + sprites.

---

## MVP Scope

### Included in MVP

1. View toggle (Kanban ↔ Pixel Office)
2. Canvas 2D game loop with delta-time
3. Auto-generated office layout (walls, floor, desks based on ticket count)
4. Code-defined character sprites (16×24px, 8 frames per base)
5. Office Workers theme pack (4–6 palette-swapped variants)
6. Character state machine mapped to ticket states
7. Walking animations with BFS pathfinding (lobby → desk → exit)
8. Speech bubbles with ticket key + state icon
9. Click character → DOM popup card (same info as kanban card)
10. Sound effects via Web Audio API (toggleable, off by default)
11. localStorage persistence (view preference, theme, sound settings)

### Post-MVP Improvements

| # | Feature | Description |
|---|---------|-------------|
| 1 | Ninja Theme Pack | Alternative character sprites — masked ninjas with colored outfits |
| 2 | Working Hours Integration | Outside working hours: lights dim, characters move to rest area (couch, sleeping). Night sky through windows |
| 3 | Background Music | Lofi/chiptune ambient track via Web Audio API oscillators. Volume slider |
| 4 | More Furniture | Coffee machine, whiteboard (run progress), server rack, plants |
| 5 | Max Workers = Desk Count | If `max_parallel=3`, show 3 active desks. Extra queued characters wait in lobby |
| 6 | Mobile Pixel Office | Responsive canvas scaling for 375px. Pinch-to-zoom |
| 7 | Preset Layouts | Small/medium/large office presets |
| 8 | Particle Effects | Confetti when all tickets complete, smoke on error |
| 9 | Custom Theme Packs | Define your own character pack via settings |
| 10 | Day/Night Cycle | Office lighting changes based on actual time of day |

---

## Open Questions

1. **Character limit:** Should we cap at ~12 characters on screen, or let it grow indefinitely?
2. **Zoom controls:** Mouse wheel zoom + pan, or fixed zoom?
3. **Kanban drag-drop in pixel view:** Should users be able to drag characters between zones to manually change ticket state, or is that kanban-only?
