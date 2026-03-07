# Pixel Office — Implementation Plan

**Design doc:** `docs/plans/2026-03-07-pixel-office-design.md`
**Target file:** `static/index.html`
**Estimated additions:** ~1200–1500 lines of JavaScript + ~50 lines HTML + ~30 lines CSS

---

## Implementation Phases

The work is split into 7 sequential phases. Each phase produces a testable increment.

---

### Phase 1: Foundation — View Toggle + Canvas Setup
**Goal:** Toggle between Kanban and Pixel Office views with an empty canvas.

**Tasks:**
1. Add `boardView: 'kanban'` to Alpine.js data object
2. Add toggle button group in board header: `Kanban | Pixel Office`
3. Save/restore `boardView` to localStorage
4. Add `<canvas id="pixelOfficeCanvas">` wrapped in a container div, shown when `boardView === 'pixel'`
5. Hide kanban columns when pixel office is active (and vice versa)
6. Set up basic game loop: `requestAnimationFrame` + delta time + `imageSmoothingEnabled = false`
7. Canvas auto-sizes to fill container, respects `image-rendering: pixelated`
8. Pause game loop when kanban view is active or tab is hidden (`document.hidden`)

**Testable result:** Toggle button switches between kanban (existing) and a black/empty canvas.

---

### Phase 2: Tile System + Office Layout
**Goal:** Render an auto-generated office with floor, walls, and desks.

**Tasks:**
1. Define tile types as constants: `FLOOR`, `WALL`, `DESK_LEFT`, `DESK_RIGHT`, `CHAIR`, `DOOR`, `EXIT`
2. Code-define tile sprites as 16×16 pixel arrays:
   - Floor tile (simple repeating carpet/wood pattern)
   - Wall tile (top border with 3D depth shading)
   - Desk tile (desk surface with monitor — 2 tiles wide: left half + right half)
   - Chair tile (simple chair, directional)
   - Door tile (entrance marker)
3. Render tile arrays to off-screen canvases at startup (sprite cache)
4. Implement `generateOfficeLayout(ticketCount)`:
   - Calculate desk count = ticket count (min 3)
   - Arrange desks in rows of 3, with spacing
   - Surround with walls on top/sides
   - Place door (lobby entrance) at top-left
   - Place exit area at bottom-right
   - Return 2D tile grid + desk positions + lobby position + exit position
5. Render tile grid to canvas each frame using cached sprites
6. Re-generate layout when ticket count changes

**Testable result:** Auto-generated pixel office with floor, walls, and desks visible on canvas. Adding tickets grows the office.

---

### Phase 3: Character Sprites + Palette Swap
**Goal:** Code-defined character sprites with color variants.

**Tasks:**
1. Define base character as 16×24 pixel arrays (color index per pixel):
   - `IDLE_0`, `IDLE_1` (2 frames — subtle breathing)
   - `WALK_R_0`, `WALK_R_1` (2 frames — right-facing walk cycle)
   - `SIT_TYPE_0`, `SIT_TYPE_1` (2 frames — typing at desk)
   - `SIT_READ_0`, `SIT_READ_1` (2 frames — reading/planning)
2. Define color palette for base character:
   ```
   [transparent, skin, hair, shirt, pants, shoes, outline, desk_overlap]
   ```
3. Implement palette swap: define 4–6 variant palettes (different hair/shirt/pants colors)
4. At startup, render all frames × all variants to off-screen canvases → sprite sheet cache
5. Implement `drawCharacter(ctx, variantId, frameName, x, y)` using cached sprites
6. Implement horizontal mirror for walk-left frames (flip the walk-right sprites)
7. Implement code-enhanced states:
   - Error: draw idle frame + semi-transparent red overlay
   - Celebrate: draw idle frame with Y-bounce offset (sinusoidal)

**Testable result:** Multiple colored characters rendered on the office floor, cycling through animation frames.

---

### Phase 4: Character State Machine + Movement
**Goal:** Characters move between zones based on ticket state, with BFS pathfinding.

**Tasks:**
1. Create `CharacterManager` class:
   - Maps `ticketId → character` (position, state, assignedDesk, path, pathIndex, variant)
   - Syncs with Alpine.js ticket data each frame
2. Character state machine:
   ```
   SPAWN → IDLE_LOBBY → WALK_TO_DESK → SIT_READ → SIT_TYPE → STAND_UP → WALK_TO_EXIT → CELEBRATE → DESPAWN
                                                        ↓
                                                      FAILED (stays seated, red tint)
                                                        ↓
                                                      RETRY → WALK_TO_LOBBY
   ```
3. Map ticket state changes to character state transitions:
   - Ticket appears (any state) → SPAWN at door
   - QUEUED → IDLE_LOBBY (stand near door)
   - PLANNING → WALK_TO_DESK → SIT_READ
   - DEVELOPING → SIT_TYPE (same desk, just change animation)
   - REVIEW/DONE → STAND_UP → WALK_TO_EXIT → CELEBRATE → DESPAWN
   - FAILED → FAILED (red tint + stay seated)
   - FAILED → QUEUED (retry) → STAND_UP → WALK_TO_LOBBY → IDLE_LOBBY
4. Implement BFS pathfinding on tile grid:
   - Input: start tile, end tile, tile grid (with blocked tiles)
   - Output: array of tile positions forming shortest path
   - Characters walk tile-by-tile at 48px/sec with smooth sub-pixel interpolation
5. Desk assignment:
   - When ticket enters PLANNING, assign next free desk
   - Free desk when ticket reaches DONE/FAILED+DESPAWN
6. Animation frame selection based on state:
   - IDLE_LOBBY: alternate idle_0/idle_1 every 0.5s
   - WALK_*: alternate walk_0/walk_1 every 0.15s, face movement direction
   - SIT_TYPE: alternate sit_type_0/sit_type_1 every 0.3s
   - SIT_READ: alternate sit_read_0/sit_read_1 every 0.3s
   - FAILED: idle_0 with red tint
   - CELEBRATE: idle_0 with Y-bounce

**Testable result:** Characters spawn at door, walk to desks when tickets start, type/read at desks, walk to exit when done. Full lifecycle visible.

---

### Phase 5: Speech Bubbles + Click Interaction
**Goal:** Speech bubbles above characters, click to show popup card.

**Tasks:**
1. Canvas-rendered speech bubbles:
   - Small pixel-art rounded rectangle above character head
   - Contains: state icon + ticket key (e.g., "⌨️ MC-9173")
   - Icons per state: ⏳ (queued), 📖 (planning), ⌨️ (developing), 📋 (review), ✓ (done), ! (failed)
   - Fade in/out over 0.5s on state transitions
   - Use `ctx.fillText()` with a small pixel font or draw text pixel-by-pixel
2. Canvas click detection:
   - `canvas.addEventListener('click', ...)` → translate mouse coords to tile coords (accounting for zoom+pan)
   - Check if click hit any character's bounding box (16×24 area)
   - If hit: set `pixelOffice.selectedCharacter = ticketId`
3. DOM popup card overlay:
   - Positioned absolutely over canvas at character's screen position
   - Same content as kanban card: ticket key, summary, state badge, elapsed time, repo, branch
   - Buttons: "Open Terminal", "Pause/Resume", "Delete"
   - Click outside → dismiss
   - Reuse existing kanban card action methods (openInteractiveTerminal, pauseTicket, etc.)
4. Hover highlight:
   - On mousemove, detect character under cursor
   - Draw subtle highlight/outline around hovered character

**Testable result:** Speech bubbles visible above all characters. Click a character → popup card with full ticket info and action buttons.

---

### Phase 6: Zoom, Pan + Drag to Change State
**Goal:** Navigate large offices and drag characters to change ticket state.

**Tasks:**
1. Camera system:
   - `camera = { x, y, zoom }` — world offset and scale
   - Apply camera transform before rendering: `ctx.setTransform(zoom, 0, 0, zoom, -camera.x * zoom, -camera.y * zoom)`
2. Zoom (mouse wheel):
   - `canvas.addEventListener('wheel', ...)` → adjust `camera.zoom`
   - Integer zoom levels: 1×, 2×, 3×, 4× (pixel-perfect)
   - Zoom toward mouse cursor position
   - Clamp to min/max zoom
3. Pan (click-drag on empty space):
   - `mousedown` on empty tile → start panning
   - `mousemove` → update `camera.x`, `camera.y`
   - `mouseup` → stop panning
   - Distinguish from character click (click on character = popup, drag on empty = pan)
4. Drag character to change state:
   - `mousedown` on character → start drag
   - Show ghost character following cursor
   - Define drop zones: lobby area = QUEUED, desk area = keep current, exit area = DONE
   - On drop in valid zone → call existing API to transition ticket state
   - Visual feedback: highlight valid drop zones while dragging
5. Coordinate transforms:
   - Screen → world: `worldX = (screenX + camera.x * zoom) / zoom`
   - World → screen: `screenX = worldX * zoom - camera.x * zoom`
   - All click/hover/drag detection uses world coordinates

**Testable result:** Scroll to zoom, drag to pan around office. Drag characters between zones to change ticket state.

---

### Phase 7: Sound System + Theme Packs + Polish
**Goal:** Web Audio sounds, Office Workers + Ninja theme pack, final polish.

**Tasks:**
1. Sound manager using Web Audio API:
   - `SoundManager` class with `audioContext`, volume control
   - Sound generators (no audio files):
     - `playChime()` — ascending 3-note (C5-E5-G5), 0.5s duration → ticket starts
     - `playKeyboard()` — rapid quiet clicks (noise burst), looping → while DEVELOPING
     - `playSuccess()` — ascending arpeggio (C5-E5-G5-C6), 0.8s → ticket done
     - `playError()` — low descending tone (C3-A2), 0.5s → ticket failed
     - `playAmbient()` — subtle filtered noise hum, continuous → background (optional)
   - Master toggle + volume (0–100%)
   - Trigger sounds on character state transitions
2. Theme system:
   - `THEMES` object containing sprite definitions per theme:
     ```
     { 'office': { palette: [...], frames: {...} }, 'ninja': { palette: [...], frames: {...} } }
     ```
   - Ninja theme: define character frames with mask, headband, outfit details
   - Theme selector dropdown in pixel office toolbar
   - On theme change: regenerate sprite sheet cache, redraw all characters
   - Save selected theme to localStorage
3. Pixel office toolbar (DOM, top of canvas container):
   - `[Theme: Office Workers ▼]  [🔊 Off]  [Volume: ███░░]`
   - Styled with Tailwind to match app aesthetic
4. Polish:
   - Smooth camera transitions (lerp on zoom changes)
   - Character Z-sorting (characters lower on screen drawn on top)
   - Monitor glow animation on desks (2-frame cycle)
   - Entry/exit door animation (character fades in/out at edges)
   - Proper handling of page resize → recalculate canvas dimensions

**Testable result:** Full MVP — sounds play on ticket events, switch between Office/Ninja themes, smooth zoom/pan, polished animations.

---

## Phase Dependencies

```
Phase 1 (Canvas setup)
  └──► Phase 2 (Tiles + layout)
         └──► Phase 3 (Character sprites)
                └──► Phase 4 (State machine + movement)
                       └──► Phase 5 (Bubbles + click)
                              └──► Phase 6 (Zoom/pan + drag)
                                     └──► Phase 7 (Sound + themes + polish)
```

Each phase builds on the previous. No phase can be skipped.

---

## Testing Strategy

| Phase | How to Test |
|-------|-------------|
| 1 | Toggle button switches views. Canvas appears/disappears. Game loop starts/stops. |
| 2 | Office renders with correct desk count. Adding tickets grows the office. |
| 3 | Characters render with different colors. Animation frames cycle correctly. |
| 4 | Start a run → watch characters spawn, walk to desks, type, walk to exit. Test all ticket state transitions. |
| 5 | Click character → popup appears with correct info. Hover shows highlight. Action buttons work. |
| 6 | Scroll to zoom in/out. Drag to pan. Drag character to lobby/exit → ticket state changes. |
| 7 | Toggle sound on → hear chimes on ticket transitions. Switch theme → characters change appearance. |

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Sprite art quality — code-defined pixels might look rough | Start with simple, clean 16×24 designs. Iterate on pixel art after engine works. |
| Performance with many characters | Cache sprites as off-screen canvases. Only redraw moving characters. Pause loop when hidden. |
| index.html getting too large | Keep sprite data compact (color index arrays). Consider extracting to a `<script>` tag loaded from separate file if >2000 lines added. |
| BFS pathfinding slow for large grids | Grid is max ~24×16 = 384 tiles. BFS is O(n) — trivially fast. |
| Canvas click detection accuracy | Use character bounding box (16×24) + zoom/pan transform. Test at all zoom levels. |
