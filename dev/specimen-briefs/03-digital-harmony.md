# Commission 03 -- Digital Harmony

**A John Whitney harmonic mandala: thousands of dots obeying one law.**

| | |
|---|---|
| Build as | a container COMP at your project root named `digital_harmony` |
| Discipline / difficulty | GLSL POP + additive sprites / advanced |
| Aspect / resolution | **1:1 -- 1080x1080** (may raise toward 1440 only if the perf gate stays green; `out1` carries the native size) |
| Settle before capture | ~240 frames |
| Envoy muscles shown | GLSL POP parametric motion, Point Sprite additive rendering, post-render feedback trails, param-bound uniforms |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

John Whitney -- Permutations (1968), Arabesque (1975), and the book Digital
Harmony (1980) -- is the father of computer motion graphics. His insight:
give hundreds of points ONE simple angular law where each point's phase
advances proportionally to its index, and let differential motion do the
composing. As the governing ratio sweeps, the field passes through chaos --
and every time the ratio crosses a rational number p/q, a q-fold rose
crystallizes out of nowhere, holds, and dissolves toward the next. His
brother James's Lapis (1966) did it as a meditative dot mandala.

Build the law, not an imitation: 2000-4000 incandescent dots in a square
void, positions computed per-frame from index and a slowly sweeping harmonic
ratio, rendered as additive sprites with comet trails. The piece breathes
between interference haze and razor-sharp petal symmetry. When the petals
lock in, it should feel like the universe snapping into tune.

## Study (what to take from Whitney)

- **Differential motion is the whole engine**: point i sits at angle
  proportional to i times a global ratio. No randomness, no forces, no noise.
  Order emerges and dissolves purely from arithmetic.
- **The rational-crossing event**: sweeping the ratio through 1/3, 2/5, 1/2,
  5/8... makes 3-, 5-, 2-, 8-fold figures crystallize. The sweep must be slow
  enough that each figure has its moment of perfect lock.
- **Lapis's temperament**: meditative, center-anchored, incandescent points on
  deep indigo darkness -- devotional, not techno.

## Look Targets (grade each 0-10; ship at 8+)

1. At least two unmistakable crystallization events per 60 seconds: a q-fold
   rose/mandala locks in sharply enough to COUNT the petals, holds, then
   dissolves through interference toward the next.
2. Dots read as individual incandescent points with short comet trails --
   not a continuous nebula smear, not a fog.
3. The void stays void: deep indigo-black floor intact; bloom never mists the
   frame; the mandala owns 60-80% of the square, centered.
4. Motion is precise and inevitable -- mathematical, hypnotic, zero jitter,
   zero randomness.
5. Color is disciplined: amber-gold body, white-hot only where dots bunch
   dense, a cool accent (rose/cyan) appearing only in high-interference
   moments.
6. A 15-second watch is always alive: global rotation + radial breathing keep
   the field moving even mid-sweep between rational locks.

## Anti-Goals (any one violated = not done)

- Any randomness or noise forces. This is the anti-murmuration: purely
  parametric, deterministic, exact.
- Starfield scatter (uniform random-looking dot spread) at ANY moment -- even
  peak interference must show structured spiral/moire order.
- Nebula wash: trails so long or bloom so wide the individual points vanish.
- Rainbow cycling. The palette is a temperature story, not a hue wheel.
- Flocking, wander, or organic drift -- nothing here is alive; it is tuned.

## Palette

Void: deep indigo-black `#060612` with the faintest indigo lift `#0B0B22`
toward center (barely perceptible). Dots: amber-gold `#F2B33D` body, cores
toward `#FFF3D6` at density peaks, interference accents dusty rose `#C4586B`
or cold cyan `#58C6C0` (choose ONE accent, driven by harmonic tension).
Finish: tight bloom, subtle grain, a whisper of teal in the shadows.

## The law (implement exactly, then tune constants)

For point i of N, time t:

- `ratio(t)` sweeps SLOWLY and smoothly through a designed path that lingers
  near rationals: e.g. 0.5 -> 0.618 -> 0.333 -> 0.4 -> 0.25 ... (ease into
  and out of each rational so the lock has dwell time).
- angle: `theta_i = 2*pi * i * ratio(t) + rot(t)` where `rot` is a slow
  global rotation (one revolution per ~90s).
- radius: `r_i = R * sqrt(i/N) * breathe(t) * (1 + a*sin(m*2*pi*i/N + phi(t)))`
  -- sqrt spread fills the disc evenly; a gentle m-lobed radial harmonic and a
  slow breathing envelope keep it dimensional.
- Color/scale per point from local harmonic tension (distance of `ratio` to
  the nearest small rational) and radius.

Tune `a`, `m`, dwell times, and the sweep path until the crystallization
events land. This law is the piece's soul -- spend your iterations here.

## Technique spine (latitude allowed; the look is the contract)

- N points from a Grid/Line POP -> GLSL POP compute writes `P` per frame from
  the law (auto-declared uniforms bound to the COMP params; `outputattrs`
  set). Add `Color` and `PointScale` attributes via the New Attribute
  sequence.
- Render: Point Sprite MAT, additive blend, radial-falloff sprite texture,
  per-point scale small (1-3px class) -- keep per-sprite brightness low and
  let bloom carry the glow.
- Trails: Feedback TOP AFTER the Render TOP -- previous frame multiplied by
  0.90-0.96, tiny blur, composited under the fresh frame. (Trail length is a
  param; keep decay bounded, Reset wired.)
- Post: bloom, gentle grade, grain. Square render resolution throughout.
- The ratio sweep and breathing envelope live in a small named CHOP network
  (`ratio_sweep`, `breathe`, `rotation`) exported into the uniforms -- the
  harmony must be inspectable, not buried in the shader.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Points` | 500-4000 (int) | Voice count |
| `Sweep` | 0.1-2.0 | Ratio-sweep rate (crystallization frequency) |
| `Trails` | 0-1 | Feedback persistence (0 = crisp dots, 1 = long comets) |
| `Glow` | 0-1 | Bloom/brightness budget |
| `Hue` | 0-1 | Rotates the gold body tone around the temperature story |

## Notes

- Load `/pop-networks` before the POP work. `targetpop` is NOT needed here --
  positions are computed absolutely each frame with no integration, so there
  is no feedback loop in the POP graph; that simplicity is a feature worth
  calling out.
- Verify crystallization on camera: capture during a rational dwell and count
  the petals in the frame. If you cannot count them, the sweep is too fast or
  the trails too long.
- Verify determinism: two captures at the same `ratio` value (one sweep
  apart) should show the same figure at a different global rotation.
- Hero frame: a locked 5-fold rose with fresh short trails, counted and
  confirmed on the captured frame.
