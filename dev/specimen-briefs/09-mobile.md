# Commission 09 -- Mobile

**A Calder mobile: wind for a motor, the shadow a second composition.**

| | |
|---|---|
| Build as | a container COMP at your project root named `mobile` |
| Discipline / difficulty | 3D hierarchy, physics-feel, and shadow light rig / advanced |
| Aspect / resolution | **3:2 -- 1620x1080** (`out1` carries the native size) |
| Settle before capture | ~240 frames (the air must settle into believable swing) |
| Envoy muscles shown | Hierarchical parent transforms, CHOP physics-feel (spring and lag, no engine), and a shadow-casting light rig -- Envoy rigs an articulated OBJECT, not fields and particles |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Alexander Calder (1898-1976) put sculpture in motion. Marcel Duchamp coined
the word "mobile" for his moving pieces in 1931, and the wind-driven works
that followed are the canon -- Lobster Trap and Fish Tail (1939) hangs in a
stairwell at MoMA: a wire trap-form counterweighting a cascade of fish-tail
petals, asymmetric, air-stirred, never repeating twice.

Build a Calder in a sunlit room: wire arms and flat cut-metal petals in
Calder red, black, and white -- yellow and blue sparingly -- hung in a raking
afternoon sun that throws long hard shadows onto a warm plaster wall. The
shadow IS the second composition, dancing with its object. Ambient air keeps
every tier in glacial counter-rotation; a gust every minute or two stirs the
cascade, and the system finds equilibrium again.

The badass here is physics-feel without a physics engine: believable
balance, a gust impulse traveling down the tiers, a settle with no bounce --
all authored in CHOPs and parent transforms. And every motion is judged
twice: once on the object, once on the wall.

## Study (what to take from Calder)

- **The asymmetric balance grammar**: at each pivot, one short heavy side
  counters one long light side; the composition cascades down 3-5 tiers.
  Nothing is evenly spaced -- everything is exactly counterweighted.
- **Petal shapes**: organic cut-metal leaves, paddles, and discs -- always
  flat, always matte, edges honest.
- **The color law**: mostly black and red, with white, yellow, and blue used
  sparingly. Color is punctuation, not decoration.
- **Wind as choreographer**: tiers counter-rotate at different glacial rates,
  and a stir arrives as an impulse traveling down the cascade.
- **The shadow doubles the piece**: the wall behind a Calder is as composed
  as the air around it.

## Look Targets (grade each 0-10; ship at 8+)

1. The silhouette reads as Calder: an asymmetric cascade of organic petals on
   wire arms -- never a nursery mobile, never a symmetric chandelier.
2. Balance is believable: arms sit level or gently tilted; tiers
   counter-rotate independently with periods in the 20-90s class; nothing
   ever spins like a fan.
3. A gust event reads on camera: an impulse visibly travels down the tiers --
   heavy petals swing slow while small ones answer quick -- and the system
   settles over 20-40s with no residual bounce (prove it with timed captures).
4. The shadow composition co-stars: hard-edged, elongated petal shadows on
   the wall, moving with their objects, deliberately composed into the frame.
5. Material truth: flat matte color under one hard warm sun plus a soft low
   fill -- no gloss, no specular highlights, no gradients on petals.
6. The color law holds in every frame: mostly black and red, with yellow and
   cobalt on at most one petal each.
7. The room reads warm and real: a plaster wall with subtle tonal variation
   falling off toward the corners -- a place, not a render viewport.

## Anti-Goals (any one violated = not done)

- Carousel or ceiling-fan rotation: constant angular velocity is dead motion.
- Cartoon spring-bounce or dangling-on-elastic wobble; damped, never rubbery.
- Glossy metal, chrome, or PBR speculars -- this is painted sheet steel.
- A symmetric, evenly-spaced mobile design -- if it would hang over a crib,
  start over.
- Scene clutter: no floor, no ceiling, no window frames. The stage is a wall,
  light, and the object.
- Flat shadowless lighting -- no raking sun means no second composition.

## Palette

Warm plaster wall `#E6DCC8` falling toward `#C9BBA0` at the corners. Petals:
Calder red `#C8341E`, ink black `#16130F`, bone white `#F2EDE2`, yellow
`#E3B33A`, cobalt `#2D4C8E` -- yellow and cobalt on at most one petal each.
Wire arms near-black. Sunlight warm `#FFE9C4`; shadow cores cooling toward
`#B8A98E`. Finish: gentle grade, faint grain, protect the warm wall.

## Wind score

Two layers, always both:

- **Ambient (the drone)**: every tier carries a slow base rotation about its
  hang axis -- 20-90s periods in harmonic-ratio relationships, adjacent tiers
  counter-rotating -- plus a micro-sway pendulum tilt of a degree or two.
  `Breeze` scales both. The composition never repeats and never stops.
- **Gust (the event)**: every minute or two an impulse fires at the root and
  travels DOWN the cascade with per-tier delay and mass-scaled amplitude --
  the big petal barely nods, the small ones answer wide and quick. Each tier
  sways and settles over 20-40s, amplitude only ever decreasing.

## Technique spine (latitude allowed; the look is the contract)

- A hierarchy of Geometry/Null COMPs -- root hang point -> tier arms ->
  petals -- where each pivot is a parent transform: slow rotation about the
  hang axis plus a small pendulum-tilt axis. The hierarchy IS the engineering.
- Motion is a layered CHOP system, NOT a physics engine: per-tier base
  rotations at harmonic-ratio periods; gust impulses fired through
  Trigger/Spring CHOP chains cascading tier to tier with per-tier delay and
  mass-scaled amplitude (the Spring CHOP carries the sway-and-settle --
  verify its parameter names against the wiki), all summed into the pivot
  rotations.
- Petals as flat SOP shapes with organic cut-metal outlines and matte
  constant-style shading; wire arms as thin near-black strands.
- The rig: ONE shadow-casting sun light (raking, warm) plus a very low fill,
  with the wall as a large receiving plane -- verify the current TD shadow
  setup (light shadow type, casting flags) against the wiki before building.
- Camera static or near-static, composed for object plus shadow together.
- Post kept to a gentle grade and grain -- the sun does the finishing.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Breeze` | 0-1 | Ambient air energy (base counter-rotation and micro-sway) |
| `Gust` | 0-1 | Gust frequency (0 = still air) |
| `Sun` | 0-1 | Sun angle walk: high noon -> late raking afternoon |
| `Warmth` | 0-1 | Light and wall warmth grade |
| `Weight` | 0.5-1.5 | Petal mass feel (scales sway periods and settle time) |

## Notes

- Design the mobile on paper FIRST as a balance diagram -- tiers, arm
  lengths, petal sizes -- before creating a single op. The asymmetric cascade
  IS the composition; a network built ad hoc will hang symmetric.
- Verify counter-rotation and gust-settle on camera with t / t+15s / t+45s
  captures: the first pair proves independent tier motion, the triple proves
  the settle.
- Performance is light here (a dozen petals and one shadow map), but gate
  after the light rig anyway.
- Gusts arrive minutes apart at default settings: temporarily raise `Gust`
  to force an event for the capture evidence, then restore it before the
  review gate -- never wait blind and never fake the settle.
- Hero frame: late-afternoon sun, a gust just past -- petals mid-sway,
  shadows stretched long across the wall.
