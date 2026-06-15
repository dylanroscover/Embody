// Browser-only reactions UI: a themed emoji picker popover plus delegated
// toggling of reaction chips. One delegated listener covers the whole scope, so
// it works for SSR cards, client-appended cards, and the detail page alike. The
// palette and cluster markup come from reactions.ts so server and client agree.

import { REACTION_EMOJIS, isReactionEmoji, reactionsClusterHtml } from "./reactions";

// POST /api/specimens/:slug/react response.
interface ReactResponse {
  emoji: string;
  reacted: boolean;
  reactions: Record<string, number>;
  mine: string[];
  total: number;
}

export interface InitReactionsOptions {
  /** Where to delegate clicks from. Defaults to document. */
  scope?: Document | HTMLElement;
  /** Whether a user is signed in. Anonymous clicks bounce to /signin. */
  signedIn: boolean;
}

export function initReactions(options: InitReactionsOptions): void {
  const scope: Document | HTMLElement = options.scope ?? document;
  const root: HTMLElement = scope instanceof Document ? scope.body : scope;
  if (root.dataset.reactionsBound === "1") return;
  root.dataset.reactionsBound = "1";

  let popover: HTMLElement | null = null;
  let activeCluster: HTMLElement | null = null;
  let activeTrigger: HTMLElement | null = null;

  function signInRedirect(): void {
    const next = window.location.pathname + window.location.search;
    window.location.href = `/signin?next=${encodeURIComponent(next)}`;
  }

  function buildPopover(): HTMLElement {
    const el = document.createElement("div");
    el.className = "reaction-popover";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "Pick a reaction");
    el.hidden = true;

    const grid = document.createElement("div");
    grid.className = "reaction-popover__grid";
    for (const emoji of REACTION_EMOJIS) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "reaction-popover__emoji";
      button.dataset.emoji = emoji;
      button.setAttribute("aria-label", `React ${emoji}`);
      button.textContent = emoji;
      grid.appendChild(button);
    }
    el.appendChild(grid);
    document.body.appendChild(el);

    grid.addEventListener("click", (event) => {
      const target = event.target as Element | null;
      const button = target?.closest<HTMLElement>("[data-emoji]");
      if (!button || !activeCluster) return;
      const cluster = activeCluster;
      const emoji = button.dataset.emoji ?? "";
      closePopover();
      void toggle(cluster, emoji);
    });
    return el;
  }

  function positionPopover(trigger: HTMLElement, el: HTMLElement): void {
    const rect = trigger.getBoundingClientRect();
    const margin = 8;
    const width = el.offsetWidth;
    const height = el.offsetHeight;

    let left = rect.left;
    if (left + width > window.innerWidth - margin) left = window.innerWidth - margin - width;
    if (left < margin) left = margin;

    let top = rect.bottom + 6;
    if (top + height > window.innerHeight - margin) top = rect.top - 6 - height; // flip above
    if (top < margin) top = margin;

    el.style.left = `${Math.round(left)}px`;
    el.style.top = `${Math.round(top)}px`;
  }

  function openPopover(trigger: HTMLElement, cluster: HTMLElement): void {
    if (!popover) popover = buildPopover();
    activeCluster = cluster;
    activeTrigger = trigger;
    popover.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    positionPopover(trigger, popover);
  }

  function closePopover(): void {
    if (popover) popover.hidden = true;
    if (activeTrigger) activeTrigger.setAttribute("aria-expanded", "false");
    activeCluster = null;
    activeTrigger = null;
  }

  function renderCluster(
    cluster: HTMLElement,
    reactions: Record<string, number>,
    mine: string[]
  ): void {
    const rawMax = cluster.dataset.max;
    const max = rawMax ? Number(rawMax) : undefined;
    cluster.outerHTML = reactionsClusterHtml({
      slug: cluster.dataset.slug ?? "",
      reactions,
      mine,
      max: typeof max === "number" && Number.isFinite(max) ? max : undefined
    });
  }

  async function toggle(cluster: HTMLElement, emoji: string): Promise<void> {
    if (!isReactionEmoji(emoji)) return;
    if (!options.signedIn) {
      signInRedirect();
      return;
    }
    const slug = cluster.dataset.slug ?? "";
    if (!slug) return;

    cluster.classList.remove("is-failed");
    cluster.classList.add("is-busy");
    try {
      const res = await fetch(`/api/specimens/${encodeURIComponent(slug)}/react`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emoji })
      });
      if (res.status === 401) {
        signInRedirect();
        return;
      }
      if (!res.ok) {
        cluster.classList.add("is-failed");
        return;
      }
      const data = (await res.json()) as ReactResponse;
      renderCluster(cluster, data.reactions, data.mine);
    } catch {
      cluster.classList.add("is-failed");
    } finally {
      cluster.classList.remove("is-busy");
    }
  }

  root.addEventListener("click", (event) => {
    const target = event.target as Element | null;
    if (!target) return;

    const openBtn = target.closest<HTMLElement>("[data-react-open]");
    if (openBtn) {
      const cluster = openBtn.closest<HTMLElement>("[data-reactions]");
      if (!cluster) return;
      event.preventDefault();
      event.stopPropagation();
      if (!options.signedIn) {
        signInRedirect();
        return;
      }
      const isOpenHere = activeTrigger === openBtn && popover !== null && !popover.hidden;
      if (isOpenHere) {
        closePopover();
      } else {
        openPopover(openBtn, cluster);
      }
      return;
    }

    const chip = target.closest<HTMLElement>("[data-react]");
    if (chip) {
      const cluster = chip.closest<HTMLElement>("[data-reactions]");
      if (!cluster) return;
      event.preventDefault();
      event.stopPropagation();
      void toggle(cluster, chip.dataset.emoji ?? "");
    }
  });

  // Dismiss the popover on outside press, Escape, or any scroll/resize.
  document.addEventListener("pointerdown", (event) => {
    if (!popover || popover.hidden) return;
    const node = event.target as Node | null;
    if (popover.contains(node)) return;
    if (activeTrigger && node && activeTrigger.contains(node)) return;
    closePopover();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && popover && !popover.hidden) closePopover();
  });
  window.addEventListener(
    "scroll",
    () => {
      if (popover && !popover.hidden) closePopover();
    },
    true
  );
  window.addEventListener("resize", () => {
    if (popover && !popover.hidden) closePopover();
  });
}
