// Embot, the Envoy build-bot, on the web: a port of envoy_viz.botDance() from
// the Embody extension. Same eight network-box parts (body, arms, legs, head,
// two eyes) plus the speech bubble, same hop (parabolic arc, ease-out, landing
// squash), same gesture set (wave, shrug, pump, the occasional full-body dance)
// on the same random schedule, blink, happy squint, the cool-to-warm "thinking"
// hue, and the typewriter bubble with its spinner. Coordinates are TD network
// units, y up, scaled by the root's data-scale; the figure's anchor is the body
// centre and its feet sit FIGURE_BOTTOM units below it.
//
// The class owns physics, gestures and painting. What he DOES is a driver:
// walkDriver hops him left to right across his container and back, standDriver
// keeps him put, and the hero game (HeroGame.astro) drives him tile to tile.
// One rAF loop serves every bot; it draws a single frame under
// prefers-reduced-motion and skips bots that are off-screen.

export type Driver = (bot: Embot, t: number) => void;

type Part = { name: string; ox: number; oy: number; w: number; h: number; eye: boolean; el: HTMLElement; bg: string };

// envoy_viz._VIZ_BOT_PARTS: name, centre offset x/y, base width/height, is_eye.
const PART_TABLE: Array<[string, number, number, number, number, boolean]> = [
  ["body", 0, 0, 30, 34, false],
  ["arm_l", -22, 3, 9, 26, false],
  ["arm_r", 22, 3, 9, 26, false],
  ["leg_l", -8, -29, 11, 24, false],
  ["leg_r", 8, -29, 11, 24, false],
  ["head", 0, 31, 34, 26, false],
  ["eye_l", -8, 35, 12, 13, true],
  ["eye_r", 8, 35, 12, 13, true]
];

// envoy_viz._VIZ_* (seconds, network units).
export const JUMP_DUR = 0.52;
export const ENTRANCE_DUR = 0.95;
export const JUMP_ARC = 55;
export const HOP_DWELL = 0.8;
export const HOP_MIN = 0.32;
const SQUASH = 0.07;
const GESTURE_GAP_MIN = 4;
const GESTURE_GAP_MAX = 11;
const GESTURE_DUR = 1.6;
const DANCE_DUR = 3.0;
const WAVE_LIFT = 28;
const WAVE_FREQ = 14;
const WAVE_AMP = 9;
// TD ramps over 14s (_VIZ_WARM_S) because real thinking gaps run long; the
// page's pauses are shorter, so warm faster or he never leaves cyan.
const WARM_S = 5;
const COOL_HUE = 0.58;
const WARM_HUE = 0.0;
const SQUINT_GAP_MIN = 9;
const SQUINT_GAP_MAX = 17;
const SQUINT_DUR = 1.1;
const SQUINT_FLATTEN = 0.74;
const SQUINT_WIDEN = 1.18;
// TD's bubble is 185x74 at 11px; the web one keeps the width and trims the
// height to the two lines it holds.
const BUBBLE_W = 185;
const BUBBLE_H = 46;
const BUBBLE_LIFT = 58;
export const FIGURE_BOTTOM = 41; // legs' lowest edge below the anchor (29 + 24 / 2)
export const FIGURE_TOP = 44; // eyes' top edge above the anchor (35 + 13 / 2)

// EnvoyExt._OP_DESCRIPTIONS: what Embot says about the op he just finished.
export const OP_DESCRIPTIONS: Record<string, string> = {
  noiseTOP: "seeded a noise texture",
  rampTOP: "laid down a gradient",
  constantTOP: "filled a solid colour",
  transformTOP: "repositioned the image",
  blurTOP: "softened it with a blur",
  levelTOP: "graded brightness & contrast",
  edgeTOP: "traced the edges",
  compositeTOP: "blended two layers",
  hsvadjustTOP: "shifted hue & saturation",
  feedbackTOP: "fed the output back in",
  glslTOP: "ran a GLSL shader",
  renderTOP: "rendered the scene",
  nullTOP: "marked the output",
  outTOP: "exposed the output",
  lfoCHOP: "set an oscillator going",
  mathCHOP: "scaled the signal",
  filterCHOP: "smoothed the motion",
  noiseCHOP: "added some jitter",
  nullCHOP: "marked the channel output",
  gridSOP: "built a point grid",
  noiseSOP: "displaced the geometry",
  transformSOP: "transformed the points",
  nullSOP: "marked the geometry output",
  gridPOP: "built GPU points",
  noisePOP: "displaced them on the GPU",
  nullPOP: "marked the POP output",
  phongMAT: "set up a phong material",
  geometryCOMP: "placed geometry to render",
  cameraCOMP: "set up the camera",
  lightCOMP: "added a light",
  baseCOMP: "opened a sub-network",
  webclientDAT: "wired up a web client",
  textDAT: "dropped in a text DAT"
};

// _actionText's verb fallbacks, for ops without a description.
export const CAPTIONS: string[] = [
  ...Object.values(OP_DESCRIPTIONS),
  "built noise1",
  "wired up level1",
  "tuned blur1",
  "rebuilt kandinsky",
  "worked on out1"
];

export const rand = (a: number, b: number): number => a + Math.random() * (b - a);

export function pickCaption(not: string): string {
  let c = not;
  for (let i = 0; i < 8 && c === not; i++) c = CAPTIONS[Math.floor(Math.random() * CAPTIONS.length)] ?? c;
  return c;
}

function hsv(h: number, s: number, v: number): string {
  const i = Math.floor(h * 6);
  const f = h * 6 - i;
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  const sextant: Array<[number, number, number]> = [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]];
  const [r, g, b] = sextant[i % 6] as [number, number, number];
  return `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
}

// The animation clock, in seconds. It is VIRTUAL: the shared loop advances it
// by the real frame delta clamped to one 30fps step, so a main-thread block
// holds the animation for a frame instead of teleporting it forward by the
// whole stall (the nav's html2canvas glass snapshot blocks 130ms + 290ms in
// the first seconds; font swaps and GC cost less). Everything that schedules
// -- hops, gestures, captions, the camera -- reads this, never performance.now.
const MAX_STEP = 1 / 30;
let clock = performance.now() / 1000;
let realT = clock;
export const now = (): number => clock;
function advanceClock(): void {
  const r = performance.now() / 1000;
  clock += Math.min(r - realT, MAX_STEP);
  realT = r;
}

export class Embot {
  root: HTMLElement;
  S: number;
  parts: Part[];
  bubble: HTMLElement;
  bubbleText: HTMLElement;
  floorPx: number;
  w = 0;
  h = 0;
  originY = 0;
  /** Camera offset in px, subtracted at paint (the hero game scrolls its world both ways). */
  offsetX = 0;
  offsetY = 0;
  visible = true;
  driver: Driver | null = null;
  onLand: ((bot: Embot) => void) | null = null;
  // hop state (units)
  from = { x: 0, y: 0 };
  target = { x: 0, y: 0 };
  base = { x: 0, y: 0 };
  jumpT0 = -10;
  jumpDur = JUMP_DUR;
  arc = JUMP_ARC;
  landedFired = true;
  // gesture / blink / squint schedules
  gestureType = -1;
  gestureStart = 0;
  gestureEnd = 0;
  nextGesture = 0;
  blinkEnd = 0;
  nextBlink = 0;
  squintEnd = 0;
  nextSquint = 0;
  // speech + colour
  caption = "";
  speechT0 = 0;
  lastActivity = 0;
  lastLine = "";

  constructor(root: HTMLElement, opts: { floorPx?: number } = {}) {
    this.root = root;
    this.S = parseFloat(root.dataset.scale || "1") || 1;
    this.floorPx = opts.floorPx ?? 16;
    this.parts = PART_TABLE.map(([name, ox, oy, w, h, eye]) => ({
      name, ox, oy, w, h, eye, el: root.querySelector(`[data-part="${name}"]`) as HTMLElement, bg: ""
    }));
    this.bubble = root.querySelector(".embot__bubble") as HTMLElement;
    this.bubbleText = root.querySelector(".embot__bubble-text") as HTMLElement;
    const t = now();
    this.nextGesture = t + rand(1.5, 4);
    this.nextBlink = t + rand(2, 5.5);
    this.nextSquint = t + SQUINT_GAP_MIN;
    this.resize();
  }

  resize(): void {
    const r = this.root.getBoundingClientRect();
    this.w = r.width;
    this.h = r.height;
    this.originY = this.h - this.floorPx - FIGURE_BOTTOM * this.S;
    // Base sizes are set once here; per-frame squash and squint ride on the
    // transform's scale so a frame never touches layout.
    const S = this.S;
    for (const p of this.parts) {
      p.el.style.width = `${p.w * S}px`;
      p.el.style.height = `${p.h * S}px`;
    }
    this.bubble.style.width = `${BUBBLE_W * S}px`;
    this.bubble.style.height = `${BUBBLE_H * S}px`;
  }

  /** Container width in units. */
  get W(): number { return this.w / this.S; }
  /** True once the current hop has landed. */
  get landed(): boolean { return (now() - this.jumpT0) / this.jumpDur >= 1; }
  /** Anchor y (units) that puts his feet on a surface `feetPx` from the container's top. */
  yForFeetAt(feetPx: number): number { return (this.originY - (feetPx - FIGURE_BOTTOM * this.S)) / this.S; }

  /** Teleport, no hop. */
  place(x: number, y: number): void {
    this.from = { x, y };
    this.target = { x, y };
    this.jumpT0 = -10;
    this.landedFired = true;
  }

  /** Hop from wherever he is (mid-air included) to (x, y). */
  hopTo(x: number, y: number, t: number, dur = JUMP_DUR, arcMul = 1): void {
    this.from = { ...this.base };
    this.target = { x, y };
    this.jumpT0 = t;
    this.jumpDur = dur;
    this.arc = JUMP_ARC * arcMul;
    this.landedFired = false;
  }

  /** A new action: caption, typewriter restart, colour back to cool. */
  say(caption: string, t: number): void {
    this.caption = caption;
    this.speechT0 = t;
    this.lastActivity = t;
  }

  tick(t: number): void {
    if (this.driver) this.driver(this, t);
    const S = this.S;
    // --- hop physics (botDance) ---
    const tt = (t - this.jumpT0) / this.jumpDur;
    let sx = 1, sy = 1, px: number, py: number;
    if (tt < 1) {
      const e = 1 - (1 - tt) * (1 - tt);
      px = this.from.x + (this.target.x - this.from.x) * e;
      py = this.from.y + (this.target.y - this.from.y) * e + this.arc * Math.sin(Math.PI * tt);
      if (tt > 0.82) {
        const k = (tt - 0.82) / 0.18;
        sx = 1 + SQUASH * k;
        sy = 1 - SQUASH * k;
      }
    } else {
      px = this.target.x;
      py = this.target.y;
      if (!this.landedFired) {
        this.landedFired = true;
        if (this.onLand) this.onLand(this);
      }
    }
    this.base = { x: px, y: py };
    // --- gestures at random intervals, only while standing ---
    if (tt >= 1 && t >= this.gestureEnd && t >= this.nextGesture) {
      let g: number;
      if (Math.random() < 0.18) g = 3;
      else {
        g = Math.floor(Math.random() * 3);
        if (g === this.gestureType) g = (g + 1) % 3;
      }
      this.gestureType = g;
      this.gestureStart = t;
      this.gestureEnd = t + (g === 3 ? DANCE_DUR : GESTURE_DUR);
      this.nextGesture = this.gestureEnd + rand(GESTURE_GAP_MIN, GESTURE_GAP_MAX);
    }
    const active = tt >= 1 && t < this.gestureEnd;
    const gi = this.gestureType;
    const gdur = this.gestureEnd - this.gestureStart;
    const gp = t - this.gestureStart;
    const genv = active && gdur > 0 ? Math.sin(Math.PI * (gp / gdur)) : 0;
    if (active && gi === 3) {
      px += Math.round(Math.sin(gp * 6)) * 11 * genv;
      py += Math.abs(Math.sin(gp * 9)) * 7 * genv;
    }
    // --- thinking colour: cool right after an action, warming over 14s ---
    const idle = t - this.lastActivity;
    const f = Math.min(1, Math.max(0, idle / WARM_S));
    const hue = Math.round((COOL_HUE + (WARM_HUE - COOL_HUE) * f) * 36) / 36;
    const skin = hsv(hue, 0.95, 1);
    // --- blink and squint ---
    if (t >= this.nextBlink) {
      this.blinkEnd = t + 0.13;
      this.nextBlink = t + rand(2, 5.5);
    }
    const blinking = t < this.blinkEnd;
    if (t >= this.nextSquint) {
      this.squintEnd = t + SQUINT_DUR;
      this.nextSquint = t + rand(SQUINT_GAP_MIN, SQUINT_GAP_MAX);
    }
    const squinting = t < this.squintEnd;
    // --- place the parts ---
    const ox0 = this.offsetX, oy0 = this.offsetY;
    for (const p of this.parts) {
      let ox = p.ox, oy = p.oy, gw = 1, gh = 1;
      if (active) {
        if (gi === 0 && p.name === "arm_r") {
          oy += WAVE_LIFT * genv;
          ox += Math.sin(gp * WAVE_FREQ) * WAVE_AMP * genv;
        } else if (gi === 1 && (p.name === "arm_l" || p.name === "arm_r")) {
          oy += 16 * genv;
        } else if (gi === 2 && (p.name === "arm_l" || p.name === "arm_r")) {
          oy += WAVE_LIFT * 0.75 * genv;
        } else if (gi === 3) {
          if (p.name === "arm_l") oy += 20 * genv * (0.5 + 0.5 * Math.sin(gp * 7));
          else if (p.name === "arm_r") oy += 20 * genv * (0.5 + 0.5 * Math.sin(gp * 7 + Math.PI));
          else if (p.name === "head" || p.eye) ox += Math.round(Math.sin(gp * 6)) * 4 * genv;
        }
      }
      if (p.eye && squinting) {
        gw *= SQUINT_WIDEN;
        gh *= SQUINT_FLATTEN;
      }
      const pw = p.w * sx * gw, ph = p.h * sy * gh;
      const cx = px + ox * sx, cy = py + oy * sy;
      // Transform only (compositor work): left/top/width/height writes cost a
      // layout every frame. Origin is the top-left, so translate lands the
      // scaled box exactly where the old left/top put it.
      p.el.style.transform = `translate3d(${(cx - pw / 2) * S - ox0}px, ${this.originY - (cy + ph / 2) * S - oy0}px, 0) scale(${pw / p.w}, ${ph / p.h})`;
      const bg = p.eye ? (blinking ? skin : "#000") : skin;
      if (bg !== p.bg) { p.bg = bg; p.el.style.background = bg; }
    }
    // --- speech bubble: follows the base position, not the dance sway ---
    this.bubble.style.transform = `translate3d(${this.base.x * S - (BUBBLE_W * S) / 2 - ox0}px, ${this.originY - (this.base.y + BUBBLE_LIFT + BUBBLE_H) * S - oy0}px, 0)`;
    const act = this.caption;
    const shown = act.slice(0, Math.floor((t - this.speechT0) * 45));
    let line: string;
    if (shown.length < act.length) line = shown + "_";
    else if (idle < 4) line = `${"|/-\\"[Math.floor(t * 4) % 4]} ${act}${".".repeat(Math.floor(t * 2) % 4)}`;
    else line = act;
    if (line !== this.lastLine) {
      this.bubbleText.textContent = line;
      this.lastLine = line;
    }
  }
}

/** Hop left to right across the container, exit, wait, come back. */
export function walkDriver(): Driver {
  let nextHop = 0, exitedAt = -1, started = false;
  const enter = (bot: Embot, t: number) => {
    bot.place(-110, 0);
    bot.hopTo(rand(60, 140), 0, t, ENTRANCE_DUR);
    bot.say(pickCaption(bot.caption), t);
    nextHop = t + ENTRANCE_DUR + rand(HOP_DWELL, 2.2);
    exitedAt = -1;
  };
  return (bot, t) => {
    if (!started) { started = true; enter(bot, t); return; }
    if (exitedAt >= 0) {
      if (t > exitedAt + 4) enter(bot, t);
    } else if (t >= nextHop && t >= bot.gestureEnd) {
      const x = bot.target.x + rand(70, 130);
      bot.hopTo(x, 0, t);
      bot.say(pickCaption(bot.caption), t);
      nextHop = t + JUMP_DUR + rand(HOP_DWELL, 2.4);
      if (x > bot.W + 110) exitedAt = t + JUMP_DUR;
    }
  };
}

/** Stay put, centred, and keep talking. */
export function standDriver(): Driver {
  let nextCaption = 0, started = false;
  return (bot, t) => {
    if (!started || Math.abs(bot.target.x - bot.W / 2) > 0.5) {
      bot.place(bot.W / 2, 0);
      if (!started) { started = true; bot.say(pickCaption(""), t); nextCaption = t + rand(3, 7); }
    }
    if (t >= nextCaption) {
      bot.say(pickCaption(bot.caption), t);
      // Past WARM_S he reads red: the long end of this range gets him there.
      nextCaption = t + rand(3, 7);
    }
  };
}

// --- the shared loop ---
const bots: Embot[] = [];
let loopStarted = false;
let io: IntersectionObserver | null = null;
export const reducedMotion = (): boolean =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function registerBot(bot: Embot): void {
  bots.push(bot);
  if (!io) {
    io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const b = bots.find((x) => x.root === e.target);
        if (b) b.visible = e.isIntersecting;
      }
    });
  }
  io.observe(bot.root);
  if (reducedMotion()) {
    // One composed frame: landed, caption complete, eyes open, no motion.
    bot.tick(now());
    bot.jumpT0 = -10;
    bot.speechT0 = -10;
    bot.lastActivity = -10;
    bot.gestureEnd = 0;
    bot.tick(now());
    return;
  }
  if (loopStarted) return;
  loopStarted = true;
  window.addEventListener("resize", () => { for (const b of bots) b.resize(); });
  const loop = () => {
    advanceClock();
    if (!document.hidden) {
      const t = now();
      for (const b of bots) if (b.visible) b.tick(t);
    }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}

/** Mount every [data-embot] whose mode is walk or stand; the game drives its own. */
export function mountEmbots(): void {
  for (const el of document.querySelectorAll<HTMLElement>("[data-embot]")) {
    if (el.dataset.embotMounted) continue;
    const mode = el.dataset.mode || "stand";
    if (mode !== "walk" && mode !== "stand") continue;
    el.dataset.embotMounted = "1";
    const bot = new Embot(el, { floorPx: mode === "walk" ? 28 : 16 });
    bot.driver = mode === "walk" ? walkDriver() : standDriver();
    registerBot(bot);
  }
}
