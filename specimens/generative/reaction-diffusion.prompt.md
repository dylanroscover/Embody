# Reaction-Diffusion (Gray-Scott)

A living generative texture: two virtual chemicals diffuse across the surface and
react with each other every frame, growing organic maze / coral patterns that
never stop evolving. Built entirely on the GPU with a feedback loop and a GLSL
fragment shader.

## What it teaches
- The TouchDesigner **feedback-loop** pattern (a Feedback TOP holding state between frames).
- Running a numerical simulation inside a **GLSL TOP** (the Gray-Scott equations).
- Why a feedback specimen only evolves when its output is **demanded** (viewed or rendered),
  and how `warmup_frames` lets the thumbnail bake before capture.

## How it works
1. `glsl_seed` writes the initial state: chemical A = 1 everywhere, chemical B = 1
   in a jittered grid of spots (A and B live in the R and G channels).
2. `feedback_state` (Feedback TOP) holds the previous frame's state; its Target is
   `glsl_rd_step`, which closes the loop.
3. `glsl_rd_step` applies the Gray-Scott update each frame: a 9-point Laplacian
   diffuses A and B, then the reaction `A*B*B` plus the feed/kill terms grows or
   removes B. Feed = 0.055, Kill = 0.062 (the "worms / coral" regime).
4. `glsl_colorize` maps the B concentration to a deep-navy -> teal -> cream palette
   with a soft vignette.
5. `out1` is the Out TOP that exposes the COMP's output.

All state TOPs are 512x512, 32-bit float -- reaction-diffusion needs float precision
or it bands and dies.

## Recreate it
> Build a Gray-Scott reaction-diffusion system in TouchDesigner. Use a Feedback TOP
> whose target is a GLSL TOP running the Gray-Scott equations (feed 0.055, kill 0.062,
> Da 1.0, Db 0.5) on a 512x512 32-bit-float buffer, seeded with A=1 and a scatter of
> B spots. Colorize the B channel navy -> teal -> cream and end in an Out TOP named out1.

## Tips
- Change Feed / Kill to explore regimes: spots, worms, mitosis, mazes, u-skate.
- It evolves only while `out` is cooking -- view it, render it, or drive it every frame.
