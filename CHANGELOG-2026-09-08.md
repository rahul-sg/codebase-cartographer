# Changes — 2026-09-08

Everything below came out of using the tool on a real ~175k-node estate for a day. Each fix is
paired with a regression test, and **every one of those tests was verified to FAIL against the
broken code before being trusted** — that check caught three assertions that would otherwise have
been decorative.

Test suite: 131 Python tests + a Node renderer smoke test (`tests/render_smoke.js`), which is new.

---

## Extraction and analysis

**`extract/httpcalls.py` (new)** — links frontend call sites to backend routes. Resolves
`export const X_API = '/some/path'` constants and canonicalises path params, producing
`calls-route` edges. On the test estate this connected 288 call sites at ~89% match, which is what
makes a frontend-to-database traversal possible in a few hops instead of by hand.

**`extract/topology.py`** — class-level `@RequestMapping` prefixes were missed whenever the path
came from a constant rather than a literal, and whenever the annotation was separated from the
class declaration by a comment block. Both fixed (constant resolution incl. `+` concatenation and
comma arrays; header scan now skips comment lines). Also emits `handled-by` edges (route →
controller file) with dangling-edge filtering.

**`extract/symbols.py`** — content-hash parse cache (`CACHE_VERSION`, bump on any parser
*behaviour* change, not just payload shape), plus supertype and import resolution emitting
`extends` / `implemented-by` / `imports`. Bounded lookahead handles declarations split across
lines.

**`analyze/hierarchy.py`** — edge `samples` trimmed to one evidence string. They were 68% of the
graph payload (4.31 MB of a 6.30 MB response) and read by nothing; edge detail is already fetched
on demand. Estate view dropped 3.21 MB → 1.38 MB. Also fixes a `NameError` that returned HTTP 500
on every package drill-down.

**New commands** — `ack` (record an intentional finding so it stops resurfacing; reason
mandatory), `archdiff` (architectural drift between scans), `audit` (compare raw source against
what was extracted, and report what was **missed** — two of the worst bugs found in this tool were
silent recall failures), `pr-check` (architectural context for a diff: endpoints touched, frontend
callers, multi-writer tables, reviewers by real authorship).

**`scan --only`** is now guarded: it deletes by extractor rather than by repo, so running it
against an existing graph was silently destructive.

**Timestamps** — stored UTC (correct), but displayed raw, so a scan at 16:52 local showed as 23:52.
Now rendered in the viewer's own zone with the zone labelled, in both the UI and the CLI. The label
is derived from the platform, never hardcoded — "PST" is wrong for most of the year.

---

## Renderer — correctness

These were all found from the outside, by using the thing.

1. **Two engines, one canvas.** Both bound the same five events with no view check, and the 2D pan
   and wheel handlers call `draw()` directly, bypassing the stopped animation loop — so
   interacting with the 3D view painted the 2D scene over it. Fixed with an explicit `active` flag
   owned by the view switcher.

2. **`draw()` could die permanently.** `ctx.ellipse()` throws `IndexSizeError` on a negative
   radius; the pitch clamp allowed negative pitch. The throw escaped the `requestAnimationFrame`
   callback, abandoning the frame *after* `clearRect` and *before* the next was scheduled — canvas
   blank forever, while nodes stayed clickable at stale coordinates. Fixed at four levels: valid
   clamp, non-negative radii, non-finite skips, and `draw()` wrapped in try/catch inside both
   render loops so a renderer degrades instead of dying.

3. **O(N×E) per frame.** Selection dimming called `edges.some()` inside the node loop — ~3.4M
   comparisons every frame at estate scale. Replaced with an adjacency map built once per
   selection change: **24.5ms → 3.0ms per frame**.

4. **No near plane.** `if (depth < 40) depth = 40` was a divide-by-zero guard being used as a near
   plane, so close nodes hit an 18× scale factor and single nodes 288px across covered a fifth of
   the viewport. Real near-plane culling with a fade, a radius cap, and a content-derived minimum
   distance.

5. **The force layout diverged.** With ~1200 nodes each inside the repulsion cutoff of about a
   thousand others, velocity compounds faster than damping removes it, and one coincident pair
   injects ~14,000 of force in a single step at the old separation floor. `fit()` then computed a
   scale below its own floor and the 2D view rendered everything as a speck. Fixed with the piece
   the simulation was missing — a per-iteration displacement cap (Fruchterman-Reingold's
   "temperature") — plus a sane separation floor and a non-finite rescue.

---

## Renderer — navigation

- **Movable camera pivot.** The 3D camera orbited the world origin, so zooming in swung the top and
  bottom layers out of frame permanently. The pivot now moves; every layer frames at ~7px from
  centre.
- **Layer navigator** — click a layer to aim the camera at it, `only` to isolate it, checkbox to
  hide it, `↑`/`↓` to step between floors. Isolation matters as much as the camera: reading one
  floor at a time is what makes a large estate legible.
- **Pointer-anchored zoom**, with the standard asymmetry: dolly toward the cursor going in, unwind
  toward centre coming out. Anchoring both directions leaves the view drifting off-centre, because
  offsets from two different cursor positions never cancel. Measured drift: (395, 297)px → (3, −20)px.
- **Zoom never leaves the graph** — the anchor falls back to the nearest content, and pan is
  constrained so a usable span always stays in frame. Constraining the *result* rather than the
  gesture means drag-panning is covered by the same rule.
- **Zoom range derived from content** rather than constants, so it adapts when a single layer is
  isolated.
- **Deep links** — `#view=layers&focus=<node id>`, so a finding can be *shown*, not merely cited.
- **Reset control** (`R`) and a **shortcuts panel** (`?`). Every shortcut already existed; none
  were discoverable.

---

## Renderer — appearance

Label decluttering (1143 → 139 labels drawn, and *faster*, because a thousand fewer text runs pay
for the halos), layer captions drawn last so dense views cannot bury them, a parallaxed starfield
and nebula shared by both views, additive edge blending so density reads as brightness, rim-lit
nodes, bloom on the larger ones, and a deep-space palette. Frame cost with everything on: **4.0ms**
at 1219 nodes / 2815 edges.

---

## Layer spacing: widening the floors required raising the ceiling

Sizing each floor's disc by population fixed in-layer crowding and immediately caused a worse
problem, which measurement caught and the eye had already noticed: floor heights were a fixed 140
units apart while discs now reached 950, so a floor projected a band several hundred pixels tall
into a gap of twenty. Three floors were drawn on top of one another -- the "scary" messaging layer
was not dense, it was **colliding with its neighbours**.

| | before | after |
|---|---|---|
| gap between floors | 29px | **144px** |
| messaging band height | 395px | **157px** |
| floor centres | overlapping | 53 / 273 / 455 / 599 / 728px |

Vertical spacing now scales with the widest floor (clamped, so the estate stays a building rather
than becoming a tower), disc growth was moderated from `40*sqrt(n)` to `30*sqrt(n)` because every
unit of width costs height, and the default pitch dropped from 0.46 to 0.40 — measured, floors
separate cleanly at or below 0.40 and begin to overlap above it. Labels are also budgeted per floor
(10 each, biggest first): a near edge-on layer projects to nearly a line, where grid collision alone
still permits a solid rank of text.

The lesson is the general one about coupled parameters: **the disc radius and the floor spacing were
a single design decision being maintained in two places.** Changing one without the other turned a
fix into a regression.

## Readability: give the graph room, do not delete it

The first attempt at "the map is hard to follow" hid 77% of the nodes by default. That was the
wrong fix: density was a **layout** problem, and hiding data to make a picture tidy is the tool
lying about the estate. The correct fix is space.

**Layers — per-layer disc radius.** Every floor used a hardcoded radius of 360 regardless of how
much was on it, so a 37-node floor and a 565-node floor were given identical area:

| layer | nodes | spacing before | after |
|---|---|---|---|
| services | 37 | 104 units | 104 |
| messaging | 497 | **29** | **71** |
| libraries | 565 | **27** | **71** |

Radius now scales with `sqrt(count)`, holding area-per-node roughly constant, which is what the eye
actually responds to. The layer plate and the derived zoom range follow automatically — isolating
the 565-node floor frames it at 96% of the viewport.

**Map — spread and aspect.** The repulsion ceiling was reached at about forty nodes, so every view
from there to twelve hundred got the same push and collapsed into a ball with a fringe. Ceiling
raised (safe now that the per-iteration displacement cap exists — it did not when 4200 was chosen),
edge rest length increased, horizontal gathering weakened, and repulsion biased 1.5x horizontally
because screens are wider than they are tall:

| | before | after |
|---|---|---|
| bounding box | 2556 x 2243 (aspect 1.14) | **3858 x 2391 (aspect 1.61)** |
| nearest-neighbour gap | 43px | **52px** |

`simplify` is retained but now **off by default** — hiding data is the user's choice, not the
tool's.

## Repository visibility

Colour-by-repository is **on by default**, and edges that cross a git-repository boundary are
emphasised while intra-repo edges recede. On a real five-repo estate that is 211 edges out of 1069
attributed ones -- about 20%, small enough to highlight without washing anything out, and the whole
reason to map several repositories together: coupling inside one repo can be found by reading that
repo, coupling between two is what nobody owns and nothing documents.

Keyed on `git_repo`, never on the build unit. An earlier attempt keyed on the build unit, where
every Maven module counts as its own "repo", and produced a couple of hundred meaningless seams
that were mostly shared libraries.

**Deliberately not done: repository territories / convex hulls.** 62% of edges have an endpoint with
no repository at all -- topics, tables and libraries are shared by design -- so drawing territory
would assert ownership that does not exist and overlap into mush.

## Footer

Attribution, copyright, and a restatement of the provenance rule where it is always visible. The
year comes from the clock rather than being written into the markup, and the scan stamp is repeated
because the header copy scrolls out of reach on a narrow window. Sized in the layout rather than
floated: `main` is calculated against the header and crumb bar, so a floating footer would have
quietly taken 30px off the canvas.

## Life

A codebase is not a static diagram, and the view is more honest when it shows that. Both animated
properties are tied to real measurements rather than added as decoration:

- **Edge flow** — dashes travel source to target, so call *direction* is readable without clicking,
  at a speed that rises with call volume. Only on kinds that carry traffic; structural edges
  (imports, extends) do not flow, and animating them would be a lie told for effect. `INFERRED`
  edges keep their own dash pattern, because provenance outranks decoration: a dashed line there
  means "not proven" and must not be overwritten.
- **Node pulse** — rate and amplitude from PageRank, so the parts everything depends on visibly
  beat harder. Bounded at 6% so it never changes which node looks bigger, and each node carries a
  fixed phase so the estate breathes instead of the whole canvas throbbing in unison.

Anyone who has asked their OS for reduced motion gets a completely static render, not a slower one.
Cost: Layers 4.5ms/frame, Map 2.8ms/frame — effectively free.

## Readability: dead-end pruning

The estate view was a hairball, and measurement said why: on a real 1219-node estate, **940 nodes
(77%) had degree ≤ 1**. A node attached to exactly one thing cannot lie on a path between two
services, cannot be a coupling point and cannot be a bottleneck — it carries no topology, but it
does bury the ~280 nodes that do, under a fringe of unreadable dots.

`simplify` (on by default, estate level only) hides them: **1219 → 279 nodes, 2815 → 1942 edges.**
Note the ratio — 77% of nodes removed, only 31% of edges — which is the quantitative statement of
"those nodes weren't carrying structure". It is off below estate level, where you have already
drilled to something specific and its leaves are precisely what you came for, and the current
selection is never hidden.

## Sharing

`scripts/make-share-archive.sh` builds a distributable archive and **refuses to build** unless
three gates pass: the organisation-identifier guard, the full test suite (both run against the
*staged* copy rather than the working tree, because those can differ), and an artefact sweep for
stray databases, bytecode and absolute home paths. It ships a `.gitignore` inside the archive so a
first `git init` on the receiving machine cannot commit a graph database before anyone thinks about
it.

The guard it depends on had three holes, each the same shape — **the scanned set was narrower than
the shipped set**:

1. it matched only full spellings, while real names sat in code comments as abbreviations;
2. it scanned source directories but not root-level docs;
3. it scanned root-level `.md`/`.txt` but not `.yaml`, so the example config carried a real
   workspace path — found only by unpacking the finished archive and grepping it as a stranger
   would.

A guard that is trusted before sharing is worse than no guard when it is wrong, so it is now
case-insensitive, covers abbreviations and compound identifiers, and scans every shippable file
type.

## Publishing safely

`scripts/hooks/` plus `scripts/install-git-hooks.sh` refuse any change containing a graph database,
a local `cartographer.yaml`, bytecode, or an organisation identifier. The hook reuses the suite's
own guard rather than carrying a second copy of the pattern list — two lists drift, and the one
that drifts is always the one that was trusted.

The hook matters more than CI here. By the time CI runs, the object already exists in history, and
history is the hard part to undo: rewriting it is unreliable once anything has been cloned, forked
or indexed. The hook stops the object being written at all, which is the only reliable moment.
Verified against four cases — staged database, staged local config, an identifier added to a source
comment, and an ordinary edit — refusing the first three and passing the fourth with no false
positive.

`.github/workflows/ci.yml` runs the leak check as its own first job (a failure there means
something may already be published, so it should not be buried in a larger suite's scroll), then
the test matrix on two Pythons and two operating systems, then the renderer smoke test.
`fetch-depth: 0`, because history extraction is a real code path that a shallow clone would
silently skip.

## New: Architecture tab — the runtime diagram, derived rather than drawn

The system architecture diagram is the artifact teams maintain by hand and then let go stale. This
one is derived from the graph, so it cannot be stale, and every arrow is clickable for the
`file:line` behind it.

**The gap it fills is a filter, not a renderer.** Every existing view mixes build-time coupling with
runtime interaction, and on a real estate the former drowns the latter:

| edge kind between services | arrows |
|---|---|
| **code** (imports, extends) | **422** |
| data | 39 |
| sync (HTTP/RPC) | 32 |
| build (Maven) | 14 |
| contract | 1 |

82% is compile-time structure. Filter to runtime only and 508 arrows collapse to ~154 across 37
services — average out-degree 4.2, which is a diagram. The excluded count (426,745 build-time edges
on this estate) is printed in the view, because a simplified picture that does not declare its own
filter is a lie by omission.

**Shared resources are waypoints, not shortcuts.** A queue or table between two services is drawn as
its own box with arrows through it. Collapsing producer→topic→consumer into producer→consumer would
assert a direct call that does not exist. They are also *ranked*: of 249 resources touched by 2+
services only 70 have multiple writers — the case with no documented owner — so those show by
default and the rest are one checkbox away.

**A viewport, not a scrollbox.** It shipped as a fixed-size SVG inside a scrolling `div`, so seeing
the whole architecture meant scrolling in two directions — the one thing a diagram must not require,
and scrolling is the wrong verb anyway. Fit, pan and zoom are now a single mechanism: the SVG fills
the pane and the `viewBox` decides what you see. That keeps text crisp at every scale (the reason
this view is SVG rather than canvas), opens fitted to the whole diagram, and zooms about the pointer
with **0.00px drift** — the same anchoring rule as the graph views. Zoom-out is capped at 3x the
full extent so it cannot fly off into empty space, and a drag that moved more than a few pixels
suppresses the click, or every pan starting on a box would also select it.

Found while writing the test for it: the pan handlers were registered **inside** the render
function, so each filter toggle added another pair of `window` listeners, each closing over a stale
viewBox. They are now registered once and read the live viewport through one object.

**The blank panel, and why the test missed it.** The viewport shipped broken: `render()` clears its
container, and the zoom controls were a child of that container, so the later
`insertBefore(svg, ctl)` referenced a node that was no longer attached. Browsers reject that with
`NotFoundError`, the render aborted, and the panel came up empty with a single line in the console.

The harness passed because its `insertBefore` simply pushed onto an array. **A stub looser than the
DOM tests nothing about the DOM** -- the sixth instance today of a test measuring something adjacent
to the property it claimed to check, and the second where the harness itself was the defect. The
stub now enforces real semantics: `insertBefore` throws when the reference is not a child,
`removeChild` throws when the node is not present, and `innerHTML = ""` detaches its children.
Verified by pointing it at the broken build: two clean failures instead of a crash.

**Layout, because a diagram is not a graph drawing.** Longest-path layering with cycle detection
(cycles are drawn, dashed, not treated as errors), then barycentre sweeps for crossing reduction.
Two corrections after measuring the first attempt:

| | naive | after |
|---|---|---|
| layers from a 40-service chain | 41 | **11** |
| widest row | 70 boxes (~11,000px) | **17** |

Longest-path layering is correct but literal — it produced a two-wide ribbon 17 layers tall — so
service layers are compressed proportionally (order preserved, a caller still sits above its
callee), and the data band wraps instead of forming one enormous row.

**Testing.** Six endpoint tests assert the properties that matter rather than that it returns
something: build-time edges excluded, parallel edges collapsed to one counted arrow, waypoints
ranked not dumped, queues kept as waypoints, `INFERRED` provenance winning over `EXTRACTED` on a
merged arrow (an arrow is only as trustworthy as its weakest supporting edge), and the exclusion
count reported. Verified by re-running them against a deliberately widened filter: 2 failures.

The layout is exported and asserted too — and that required extracting it into one function the
view and the test both call. The first quality probe re-implemented the layering *inside the test*,
so it measured its own copy and stayed green through a layout change. Same failure mode as the four
before it; the fix is that there is now only one implementation to measure.

## Layer plates: project the boundary, do not fake it

The grey ring under each floor was one axis-aligned `ctx.ellipse`, sized from the floor's centre
probe. Three things were wrong with that, and the first two were wrong all along — a circular disc
simply hid them:

1. **One depth for the whole ring.** Under perspective the near edge of a floor is larger than the
   far edge; a single-depth ellipse cannot show that.
2. **No yaw.** Fine for a circle, which projects to the same shape however it is spun.
3. **Rotation, once floors became elliptical.** An x-stretched, z-squashed footprint *rotates with
   yaw into a tilted ellipse*, and an axis-aligned screen ellipse cannot represent that at any size.

So the rings sat beside their own nodes. Measured containment: **as low as 50%** of a floor's nodes
falling inside the ring drawn for it.

The plate is now the *projected boundary of the actual footprint* — the same parametric curve the
nodes are placed on, sampled at 72 points and projected individually, so it is exact by
construction and correct under perspective. The stretch factors live in one place (`STRETCH_X`,
`STRETCH_Z`) used by both the placement and the plate, because that pair being duplicated is what
allowed them to diverge. Containment is now **100% across twenty camera configurations** (five yaws
x four pitches), asserted permanently in `render_smoke.js`.

**A fourth decorative-test near-miss, and the sharpest one yet.** The containment check initially
reported PASS against deliberately broken code — because it captured plates by watching path
points, and the broken renderer drew them with `ctx.ellipse`, emitting no path points at all. Zero
plates captured, nothing compared, vacuous pass. The harness now synthesises a polygon from
`ctx.ellipse` too, so both drawing styles are measured on equal terms: 50% pre-fix, 100% after.

> The recurring shape of all four: **the test measured something adjacent to the property it claimed
> to check.** Gentler fixture, emergent symptom, unrepresentative node sizes, and now an
> instrumentation blind spot. A green test is evidence only about what it actually observed.

## Exploring the stack

**Arrow-key stepping was framing the wrong thing.** `focusLayer` took its distance from
`_worldRadius()`, which measures from the pivot to the farthest node *anywhere* — so focusing the
top floor moved the pivot to its height and then measured all the way down to the bottom one. Each
press flung the camera to a different distance (1800 to 3235 across five floors) and framed the
whole stack instead of the floor requested. It now sizes from that floor's own radius, and while a
floor is the subject the whole zoom range is about that floor, so you can actually get close to
what you asked to see. Result: 686–1358 instead of 1800–3235, every floor fully on screen.

**Labels now reveal as you close in.** A fixed budget of ten is right for the estate and absurd once
you have flown down to one floor, where the labels are the whole reason you went. The budget scales
with how far inside the fit distance the camera is: **40 → 202** labels flying into the messaging
floor, still collision-culled so none overlap.

**A shorter stack, arrived at by sweep rather than by eye.** Floor separation and band height pull
in opposite directions as pitch rises — band goes with `sin(pitch)`, the projected gap with
`cos(pitch)` — so "make it shorter" and "keep the floors apart" are one coupled decision, not two.
Spacing dropped from `0.85·maxR` to `0.62·maxR` and the default pitch from 0.40 to 0.26, chosen by
sweeping the pair:

| | before | after |
|---|---|---|
| world spacing | 606 | **442** |
| canvas width used | 30% | **43%** |
| content aspect (w/h) | 0.59 | **0.78** |
| floor gap ÷ band height | 0.84 | **0.94** |

**Floors are ellipses, not circles.** A circular disc stacked vertically makes a scene far taller
than it is wide, which is why it read as "too tall" — it was a narrow column in a 16:9 frame.
Stretching each floor along x and easing it in z spends the unused width while keeping area, and
therefore crowding, essentially unchanged. `fitAll` also fits from the *measured* projection now
rather than from a spherical radius, so a wide-and-short stack no longer gets fitted to the height
and wastes the width.

## Navigation

Switching to Layers used to discard the drill-down you were in the middle of — Layers is
estate-only, so it reset level, focus and package with no way back. The position is now stashed and
restored when Map is next opened, including the selection. An explicit Reset clears the stash,
because reset should mean reset.

**Layers is deliberately not the default view.** It is the better first impression, and the wrong
tool the moment you have a specific question: it cannot drill down at all, and the 3D engine's own
header says it is "bad for tracing one specific path, because edges occlude each other". Map keeps
the estate → service → package → file descent that debugging actually needs.

## Testing

`tests/render_smoke.js` executes both real renderers against a **strict** canvas stub that enforces
what browsers enforce (radii finite and non-negative, no NaN coordinates). The previous harness used
a permissive Proxy that swallowed every call and reported PASS on a renderer that throws in Chrome —
a stub that accepts everything can only prove the code *runs*, not that its output is legal.

It sweeps 1462 camera states and asserts: no invalid geometry, every layer reachable, hidden layers
not hit-testable, zoom anchored on the pointer, zoom-out recentring, no node swallowing the
viewport, zoom staying inside the derived range, the graph never leaving frame, and a hard cap on
per-iteration layout displacement.

> **The lesson worth carrying over.** Three separate assertions in this file initially passed
> against deliberately broken code — twice because the fixture was gentler than production (nodes
> too small, all positions distinct), once because it asserted an emergent symptom that depends on
> topology a fixture cannot imitate. When a test resists being made to fail, the assertion is
> usually aimed at a symptom rather than at the guarantee. **Assert the invariant, and always run a
> new assertion against broken code before trusting it.**
