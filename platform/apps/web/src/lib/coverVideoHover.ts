// Browser-only cover-video hover controller: lazily loads and plays a card's
// cover video on hover, and clears it on leave so the poster shows through.
// One delegated pair of listeners covers the whole scope, so it works for SSR
// cards, client-appended (infinite-scroll) cards, and any future mount alike --
// no per-card listeners to leak.
//
// Additive by design: the poster <img> (thumbnail_key) always renders and never
// changes. This controller only touches the sibling <video data-cover-video>,
// whose data attribute holds the resolved /api/specimens/<slug>/video URL. The
// <video> starts with preload="none" and NO src, so nothing is fetched until a
// real hover sets the src -- keeping the grid's bandwidth/jank cost at zero for
// covers no one hovers.
//
// A hover is a DELIBERATE user action, so hover-play is NOT suppressed by
// prefers-reduced-motion (only automatic playback respects that -- see the
// detail page's autoplaying cover). It IS a no-op on a coarse/touch pointer,
// which has no hover in the grid, so those covers stay a static poster.

// The video element carries its resolved URL here (set NO src until hover).
const VIDEO_SELECTOR = "video[data-cover-video]";

// True when the pointer cannot hover (coarse / touch): there is no hover in the
// grid, so hover-play is pointless and the static poster is the whole cover. We
// deliberately do NOT gate on prefers-reduced-motion -- a hover is a deliberate
// user action requesting playback, not autoplay, so hover-play stays available
// to reduced-motion users. (Only automatic playback respects reduced motion:
// the detail page's autoplaying cover, in c/[slug].astro.)
function noHoverPointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

function playVideo(video: HTMLVideoElement): void {
  const url = video.dataset.coverVideo;
  if (!url) return;
  // On a load error (bad blob / unsupported codec / network), drop the src so the
  // poster <img> underneath shows through -- never a broken media element. Bound
  // once per video (dataset flag) since the controller is delegated, not per-card.
  if (video.dataset.coverVideoErrorBound !== "1") {
    video.dataset.coverVideoErrorBound = "1";
    video.addEventListener("error", () => stopVideo(video));
  }
  // Lazy source: preload="none" means the browser fetched nothing until now.
  if (!video.getAttribute("src")) video.setAttribute("src", url);
  // play() rejects if interrupted (fast in/out) -- swallow it, the leave
  // handler already resets state.
  void video.play().catch(() => {});
}

function stopVideo(video: HTMLVideoElement): void {
  video.pause();
  try {
    video.currentTime = 0;
  } catch {
    /* currentTime may throw before metadata loads; ignore. */
  }
  // Drop the src so the poster shows through and the buffer is released. Calling
  // load() after clearing the attribute aborts any in-flight fetch.
  if (video.getAttribute("src")) {
    video.removeAttribute("src");
    video.load();
  }
}

export interface InitCoverVideoHoverOptions {
  /** Where to delegate hover from. Defaults to document. */
  scope?: Document | HTMLElement;
}

/**
 * Wire cover-video hover-play over a scope. Idempotent per scope-root (a repeat
 * call is a no-op), and delegated, so appended cards are covered without
 * rebinding. On a coarse pointer or with reduced motion, this is a no-op and
 * every cover stays a static poster.
 */
export function initCoverVideoHover(options: InitCoverVideoHoverOptions = {}): void {
  const scope: Document | HTMLElement = options.scope ?? document;
  const root: HTMLElement = scope instanceof Document ? scope.body : scope;
  if (root.dataset.coverVideoBound === "1") return;
  root.dataset.coverVideoBound = "1";

  // Touch / coarse pointers have no hover; leave those covers as static posters.
  // (Marked bound above so a later call still short-circuits.)
  if (noHoverPointer()) return;

  // mouseenter/mouseleave don't bubble, so delegate their bubbling cousins
  // (mouseover/mouseout) and find the video for the card under the pointer.
  // mouseout also fires moving BETWEEN a card's children; guard with
  // relatedTarget still inside the same card so we only stop on a real leave.
  const videoFor = (target: EventTarget | null): HTMLVideoElement | null => {
    if (!(target instanceof Element)) return null;
    const shell = target.closest("[data-cover-shell]");
    if (!shell) return null;
    return shell.querySelector<HTMLVideoElement>(VIDEO_SELECTOR);
  };

  root.addEventListener("mouseover", (event) => {
    const video = videoFor(event.target);
    if (video) playVideo(video);
  });

  root.addEventListener("mouseout", (event) => {
    const mouse = event as MouseEvent;
    const video = videoFor(mouse.target);
    if (!video) return;
    // Still inside the same cover (moving between its children) -> not a leave.
    const shell = video.closest("[data-cover-shell]");
    if (shell && mouse.relatedTarget instanceof Node && shell.contains(mouse.relatedTarget)) {
      return;
    }
    stopVideo(video);
  });
}
