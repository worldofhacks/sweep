# Single-header application redesign — 2026-09-08

Owner direction: use the Spaces identity across every page; remove the second
network-stop/status header and permanent development-fixture band; redesign the
operational content, retain existing workflows, and anchor demo spaces in Austin.

## Delivered

- One shared brand header and unchanged primary navigation across nine pages.
- Compact stop action in the main header, with the keyboard shortcut preserved.
  Session state, connections and the explicit demo disclosure are in an on-demand
  panel. Escape closes it and returns focus; clicking outside closes it.
- Shared mineral/pine tokens, border-led cards, heading hierarchy and 44px phone
  controls. No dependency or second UI component framework was introduced.
- Control separates selection, fleet actions and movement. Ground controls,
  navigation, Live camera tiles, Speech, Gestures, Captures, Worlds, Devices/health
  and Map share the same card/control language. Worlds no longer leaves half its
  page empty because of a single-child two-column wrapper.
- Five isolated preview anchors moved to Austin. All 11 original capture records
  remain unchanged. Imported Fountain images explicitly have an Austin **demo pin**,
  not asserted Austin capture locations. Future preview seeds use Austin.
- Dedicated desktop map area; independently scrolling phone details keep the map
  visible. Explicit recentering. Map zoom 0–22, OSM source maxzoom 19 for overzooming.
  The zoom-out minimum adapts to tall viewports to match Mercator's visible-world
  limit, avoiding empty poles and an enabled control that cannot zoom further.

## Verification

- Console: **88 files, 1,226 tests passed** with two workers. The initial unrestricted
  run oversubscribed the local machine and was terminated; this is not counted as
  a passing run. Tests checking connection pills now open the new session panel.
  The fleet-free disconnect test checks disabled commands and on-demand connection
  reasons instead of requiring the removed global warning band.
- Final changed shell/map/gesture boundary: **31 tests passed** after the final
  fleet-free notice rule. ESLint, TypeScript, desktop and Android web builds passed.
- Isolated populated browser fixture: **84 page/subtab/viewport checks**, covering
  1440×1000, 768×1024, 390×844 and 320×568. No document overflow, chip overlap or
  undersized phone controls. Minimum normal working height: 334px.
- Final Live text-wrapping check retains all five fixture sources at desktop and
  both phone widths, without status-text overflow or navigation-sent commands.
  The Live module's **39 tests passed** after the CSS correction.
- Pending capture remains across all pages; no navigation action sends it. One
  explicit fixture confirmation sends exactly one capture intent. At 320×568 the
  pending pane retains 275.8px; confirm/cancel and navigation remain on screen.
  Session details do not displace the page or bottom navigation; skip link works.
- Actual Austin survey, community and hazard maps: **21 zoom/layout observations**
  passed. Both zoom controls disable at their real bounds; screenshots cover zooms
  22, 19, 14, 6 and the viewport minimum. All **1,149 observed tile responses**
  succeeded, with no request above source zoom 19. Desktop panels do not cover the
  map. After scrolling details to the bottom, maps retain 250px at 390×844 and
  170.4px at 320×568, without overlap or document overflow.
- Browser-to-simulator M14 mission passed, including speech, confirmation,
  geofence refusal, node-watchdog evidence, stop and landing. One earlier run
  timed out waiting for recovered D-01 state; a clean rerun passed without
  altering simulator or control behavior.
- Android fake/probe assemble, unit and lint tasks passed with the rebuilt Atlas
  assets. Existing warnings remain; this is not physical-device qualification.
- Python Ruff and git whitespace checks passed.

Screenshots are retained locally under `output/playwright/cohesion-fixture-*`,
`fixture-320-pending.png` and `fixture-320-session-detail.png`. Representative
Control, Live and Speech screenshots were visually inspected, as were Austin
street maps, the world-scale minimum, and persistent phone maps. The Austin map
screenshots use the `austin-*` prefix. Map source/zoom, adaptive minimum and polling
behavior also have nine unit regressions.

## Final Fleet and Health layout pass

- Fleet registry now uses the available width for responsive device cards;
  departures follow the registry instead of reserving an empty half-page column.
- Health metrics and shared services use the same border-led cards. Removed the
  forced 960px node table: all nine status fields per device wrap at phone widths.
- Fleet selection labels read “Selected”, “Select device” and “Unavailable”;
  selection restrictions, reasons and handlers are unchanged.
- Console: **91 files / 1,243 tests passed**, including new card/label assertions.
  ESLint, TypeScript, desktop and Android web builds passed. Existing bundle-size
  warnings remain; no dependency was added.
- Repeated all **84 page/subtab/viewport checks**: no layout problems, minimum
  working height 334px. Repeated pending-confirmation/navigation simulator checks:
  navigation sent nothing; explicit confirmation sent exactly one simulated intent.
- Additional four-viewport Health checks retained all 9 metrics and 9 fields on
  each of 5 fixture devices, with one header and no table/value/document overflow.
  Final desktop metric cards and 320px node details were visually inspected.
  Images: `output/playwright/health-final-*` and `cohesion-fixture-*`.

## Limits

No physical robot, camera, microphone or Android hardware operation was performed.
Unconfigured providers are not presented as live. Map imagery needs WebGL and a
reachable tile provider; this is not an offline-map guarantee. Dense reconstruction
licensing, georeferencing and remaining Atlas goal qualifications are unchanged.
