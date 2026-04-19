# Demo Storyboard — "Create at the Speed of Thought"

**Target runtime:** 60–90 seconds. Anything longer and the visceral payoff dilutes.

**Format:** Screen recording. Captions only — no voiceover. Captions are large, white, single sentence per beat, on a soft dark plate so they read against any TD network color.

**Why no voiceover:** Voiceover invites narration ("In this demo, you'll see…"). The whole point is that the *pace* of the video is the pitch. Captions force every word to earn its place.

**Why captions and not just text overlays:** Captions disappear after their beat. Text overlays linger and clutter. The screen should be 90% TouchDesigner, 10% words.

---

## Beat sheet

| # | Duration | What's on screen | Caption |
|---|---|---|---|
| 1 | 0:00–0:04 | Empty TouchDesigner network. Black. One blinking cursor in a chat panel beside it. | *(silent — let the emptiness sit)* |
| 2 | 0:04–0:09 | The user types into Claude Code: *"Build me a noise-driven particle system that reacts to audio."* Hit enter. | The gap between an idea and a network |
| 3 | 0:09–0:35 | Lightly sped up (1.5–2×): operators appear in the network one after another. Wires connect themselves. Names appear. The annotation group draws itself around the cluster. The grid stays clean. | …is where ideas die. |
| 4 | 0:35–0:38 | Fully cooked. The particle system runs. Audio reactivity visible on screen. Hold beat. | Embody collapses it. |
| 5 | 0:38–0:43 | Cut back to the chat: *"Actually, try a different approach — make it more chaotic."* | One sentence. |
| 6 | 0:43–0:55 | Sped up: the existing network rebuilds itself. Operators rearrange. Different parameters. The new version is visibly wilder than the first. | A different network. |
| 7 | 0:55–1:00 | Cut to a terminal beside the network. `git diff` of the `.tdn` file — human-readable JSON, additions in green, removals in red. Pause 2 seconds on the diff. | A diff you can read. |
| 8 | 1:00–1:08 | Back to chat: *"Show me the previous version."* The terminal runs `git checkout` and the network rebuilds itself back to the first version. | A revert in seconds. |
| 9 | 1:08–1:15 | Chat: *"Save this and start a new variant."* The terminal runs `git checkout -b variant-2`. Network unchanged. The user starts typing the next prompt — but we cut before they hit enter. | Branch off the one that works. |
| 10 | 1:15–1:25 | Black. Brand mark fades in. Soft. | **Embody. Create at the speed of thought.** |
| 11 | 1:25–1:30 | Subtitle below brand mark. Repo URL. | Open source. github.com/dylanroscover/Embody |

Total: 1:30. Trim to 1:20 if any beat feels long in the cut.

---

## Critical capture notes

These are the things that, if missed, kill the video.

### The build sequence (beats 3 and 6)

The whole video lives or dies on whether the build sequence *feels alive*. Things that ruin it:

- **Cuts that hide work.** No black flashes. No "and then suddenly everything was there." The network has to fill in continuously.
- **A messy result.** Operators must land on the grid, names must be readable, the annotation must enclose the group cleanly. If the first take produces a sloppy network, re-run the prompt — the rules in `.claude/rules/network-layout.md` should produce a clean result every time, but verify before recording.
- **Unrealistic speed.** Don't crank past 2×. Above that the eye stops registering individual operators and the magic becomes incomprehensible. The viewer needs to see *what* is being built, even sped up.

### The diff (beat 7)

This is the visceral proof. Most viewers have never seen a TouchDesigner network as text. If the diff looks like JSON soup, the moment dies. Things to do:

- **Pre-stage the terminal** with a font size large enough to read at 720p (16pt minimum, 18pt preferred).
- **Pick a `.tdn` section that diffs cleanly** — not the entire file. Maybe a single annotation group or a single operator's parameter changes. Quality over quantity.
- **Hold for two full seconds.** This is the only beat in the video where stillness is correct. Let it land.

### The revert (beat 8)

The viewer has to *feel* that the previous version came back. Two ways to sell it:

- **The audio cue.** If the first network had a particular audio-reactive behavior, the revert should bring back that exact sound. The ear notices instantly.
- **A side-by-side wipe** between the two states, optional. Risk: clutter. Skip if it doesn't add clarity.

### The brand mark (beat 10)

Single line. Single weight. No subtitle on the same beat as the line — let it sit alone for at least one full second before the URL fades in below.

The brand line is **"Embody. Create at the speed of thought."** Not "Embody — Create at the Speed of Thought." Not "Embody: Create at the Speed of Thought." A period, then the line. The period earns the gravity.

---

## What to leave out (and why)

Things that would feel natural to include but should not appear in this video:

- **A logo intro.** No. Cold open into the empty network. Trust the viewer.
- **"What is TouchDesigner?"** No. The audience for this video already knows.
- **"What is MCP?"** No. Same reason.
- **Stats.** No "45 MCP tools" or "30 test suites." That's README copy. The video is emotional, not technical.
- **Comparison to TWOZERO or any other tool.** Naming a competitor in the demo dignifies them. Win on your own terms.
- **A "team collaboration" beat.** The plan is explicit on this — solo creator in flow is the audience. Don't dilute.
- **A second prompt that fails and gets fixed.** Tempting because it shows recovery, but it adds 20 seconds and undercuts the speed promise. Save the failure-recovery story for a different video.

---

## Production checklist

- [ ] Fresh `.toe` project, nothing in the network
- [ ] Embody installed and working
- [ ] Envoy enabled, Claude Code session live and connected
- [ ] Terminal pre-staged with large font, in a directory with a clean git state and a `.tdn` file ready to diff
- [ ] Audio input plugged in (for the audio-reactive beat)
- [ ] Screen recorder set to at least 1080p, 60fps, lossless
- [ ] First prompt rehearsed once cold to make sure the layout result is clean
- [ ] Brand mark lockup and font ready as a separate overlay layer
- [ ] Caption font: large sans-serif, white on dark plate, ~32pt at 1080p

---

## After the cut

Where this video lives, in priority order:

1. **README.md** — embedded at the top, above the existing screenshot
2. **`docs/index.md`** — same placement
3. **`docs/manifesto.md`** — embedded above the first paragraph
4. **All launch surfaces** in `docs/launch-surfaces.md` — the asset they all reference

The video file itself probably belongs on a CDN or as a GitHub release asset, not committed to the repo. Decide before recording.
