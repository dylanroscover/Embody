# Multi-Agent Review (Embody dev only)

For Embody/Envoy development only. Like `commit-push-checklist.md`, `release-commits.md`, and `github-release.md`, this rule has **no shipped template counterpart** -- it is not copied into user projects.

## The rule

When you dispatch sub-agents (a Workflow, or parallel Agents) to do **substantive** work, you MUST adversarially review their output with an **independent panel of 3-5 reviewers** before you accept, integrate, commit, or report it as done. A single verify agent is not enough -- and in practice the lone verify agents in our workflows have repeatedly returned **empty verdicts** (schema drops), so one reviewer is both insufficient and unreliable.

"Substantive" = anything that builds or edits code, shaders, TD networks, data, schemas, or content via sub-agents. Trivial mechanical edits and conversational turns are exempt.

## The panel: 3-5 reviewers, DIVERSE lenses (not 3-5 identical ones)

Give each reviewer a distinct lens so they catch different failure classes:

- **Does it actually run / render?** Compile-clean is NOT correct. The crosspoint shader compiled with a `half` reserved-word error swallowed and rendered TD's red/blue fallback; `op.errors()` said "(none)" -- only the GLSL **info DAT** showed it. So: `curl` the live route, `capture_top` and READ the frame, run the test, read the info DAT, hit the endpoint. Primary evidence, not the agent's say-so.
- **Spec fidelity** -- did it do what was asked, or a plausible-looking substitute?
- **Correctness / edge cases** -- logic, nulls, the obvious bug.
- **Performance / scale at the REAL target** -- 4K (not 720p), thousands of rows (not 6), 60fps. Cheap-at-720p != cheap-at-4K; works-with-6 != works-with-thousands.
- **Security / TD safety** (when relevant) -- auth, secrets, thread/cook boundaries, no `/local`, no absolute op paths, no crash/freeze.

Use the Workflow quality patterns: perspective-diverse verify, majority-confirm (kill a finding or accept a claim only when >= a majority of reviewers agree), loop-until-dry for open-ended discovery. Scale the panel: **3** for a normal change, **5** for high-stakes work (releases, shaders bound for a conference/booth, anything touching auth / data / migrations / performance).

## The orchestrator (you) ALWAYS self-verifies on top

Never accept the panel's verdict blind -- especially because verify agents drop results. After the panel, independently confirm the **headline claims** with primary evidence yourself: typecheck, `curl` the route + grep the markers, `capture_top` + read the frame, run the suite, read the info DAT. Report what *you* verified ("`/collection` 200, 6 cards, typecheck 0 errors, captured frame shows X"), not "the agent said pass."

## Keep review reliable

- Give reviewers a **simple** return schema -- a verdict plus an issues list. Rich/nested schemas are exactly what has been dropping to empty.
- An **empty or missing verdict == NOT verified.** Treat it as a failure signal and self-verify.
- Surface every panel finding to the user with its evidence; never silently discard a reviewer's concern.
