// Shared, isomorphic (server + browser) reactions helpers. The REACTION_EMOJIS
// palette is the single source of truth for the themed picker grid, server-side
// validation, and rendering. Reaction-cluster markup is built HERE so the SSR
// card, the client-appended card template, and the specimen detail page all emit
// identical HTML. No DOM access -- the browser-only picker/popover lives in
// reactionsClient.ts. Emoji are the feature's data, so emoji literals are
// expected here; all other text stays ASCII (see ascii-punctuation rule).

// The 32 "major" reaction emojis offered by the themed picker, laid out as a
// 4x8 grid (smileys, gestures, hearts/affection, celebration/symbols). This list
// IS the allow-list -- the server rejects any emoji not in it.
export const REACTION_EMOJIS: readonly string[] = [
  "😀", "😂", "🥰", "😍", "🤩", "😎", "🤔", "😮",
  "😢", "😭", "😡", "🤯", "🥳", "😴", "🫡", "🙏",
  "👍", "👎", "👏", "🙌", "💪", "👀", "🔥", "✨",
  "🎉", "💯", "❤️", "💔", "🚀", "⭐", "🏆", "🤖"
];

const REACTION_SET: ReadonlySet<string> = new Set(REACTION_EMOJIS);

// The trigger glyph -- a smiley face, per the product ask. Exported so the client
// popover and any future surface use the same face.
export const REACTION_TRIGGER = "🙂";

export function isReactionEmoji(value: unknown): value is string {
  return typeof value === "string" && REACTION_SET.has(value);
}

// Parse a denormalized reactions_summary JSON string into a clean {emoji: count}
// map. Defensive: drops unknown emojis, non-positive, and non-finite counts so a
// malformed/legacy column can never poison the UI.
export function parseReactions(raw: string | null | undefined): Record<string, number> {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: Record<string, number> = {};
    for (const [emoji, count] of Object.entries(parsed as Record<string, unknown>)) {
      const n = Math.floor(Number(count));
      if (isReactionEmoji(emoji) && Number.isFinite(n) && n > 0) {
        out[emoji] = n;
      }
    }
    return out;
  } catch {
    return {};
  }
}

// Emoji entries sorted by count (desc), then by emoji for a stable order.
// `max` caps the list (cards show the top few; the detail page shows all).
export function topReactions(
  map: Record<string, number>,
  max?: number
): Array<[string, number]> {
  const entries = Object.entries(map)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return typeof max === "number" ? entries.slice(0, Math.max(0, max)) : entries;
}

interface ClusterOptions {
  slug: string;
  reactions: Record<string, number>;
  /** Emojis the signed-in viewer has reacted with (renders chips as "mine"). */
  mine?: readonly string[];
  /** Cap visible chips (cards). Omit to show every reacted emoji (detail page). */
  max?: number;
}

// Escape a value for safe interpolation into a double-quoted HTML attribute.
function escAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// One reaction tally chip: emoji + count, toggling that emoji for the viewer.
export function reactionChipHtml(emoji: string, count: number, mine: boolean): string {
  return (
    `<button type="button" class="reaction-chip${mine ? " is-mine" : ""}"` +
    ` data-react data-emoji="${escAttr(emoji)}" aria-pressed="${mine ? "true" : "false"}"` +
    ` title="React ${escAttr(emoji)}" aria-label="${escAttr(emoji)} reaction, ${count}">` +
    `<span class="reaction-chip__emoji" aria-hidden="true">${emoji}</span>` +
    `<span class="reaction-chip__count">${count}</span></button>`
  );
}

// The full reaction cluster: existing tally chips followed by the smiley "add"
// trigger that opens the themed picker. data-slug lets the client resolve which
// specimen a click targets without walking the DOM.
export function reactionsClusterHtml(opts: ClusterOptions): string {
  const mine = new Set(opts.mine ?? []);
  const chips = topReactions(opts.reactions, opts.max)
    .map(([emoji, count]) => reactionChipHtml(emoji, count, mine.has(emoji)))
    .join("");
  const maxAttr = typeof opts.max === "number" ? ` data-max="${opts.max}"` : "";
  return (
    `<div class="reactions" data-reactions data-slug="${escAttr(opts.slug)}"${maxAttr}>` +
    chips +
    `<button type="button" class="reaction-add" data-react-open` +
    ` aria-haspopup="dialog" aria-expanded="false" aria-label="Add a reaction">` +
    `<span class="reaction-add__face" aria-hidden="true">${REACTION_TRIGGER}</span>` +
    `<span class="reaction-add__plus" aria-hidden="true">+</span>` +
    `</button></div>`
  );
}
