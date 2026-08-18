import { useRef, useState } from "react";
import { buildEmbodyEnvelope } from "../lib/tdnEnvelope";

const COPY_RESET_MS = 1500;
const LABEL = "copy TDN for TouchDesigner";

interface Props {
  tdn: Record<string, unknown>;
  slug?: string;
  version?: number;
  className?: string;
  /** Visible button text (e.g. "Embody"). Omit for an icon-only button. */
  label?: string;
}

export default function CopyTdnEnvelopeButton({
  tdn,
  slug,
  version,
  className = "",
  label
}: Props) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);

  async function copyEnvelope() {
    if (timer.current !== undefined) {
      window.clearTimeout(timer.current);
    }

    // Safari revokes the click's transient activation across awaits, so a
    // writeText AFTER building the envelope can throw NotAllowedError. Hand
    // the PENDING build to a ClipboardItem created synchronously inside the
    // gesture (the WebKit-sanctioned pattern); engines without ClipboardItem
    // keep the plain writeText path.
    const textPromise = buildEmbodyEnvelope(tdn, { slug, version }).then(
      (envelope) => JSON.stringify(envelope)
    );

    try {
      if (typeof ClipboardItem !== "undefined" && navigator.clipboard.write) {
        await navigator.clipboard.write([
          new ClipboardItem({
            "text/plain": textPromise.then(
              (text) => new Blob([text], { type: "text/plain" })
            )
          })
        ]);
      } else {
        await navigator.clipboard.writeText(await textPromise);
      }
      setState("copied");
      timer.current = window.setTimeout(() => setState("idle"), COPY_RESET_MS);
    } catch {
      setState("failed");
      timer.current = window.setTimeout(() => setState("idle"), COPY_RESET_MS);
    }
  }

  const statusClass = state === "copied" ? "is-copied" : state === "failed" ? "is-failed" : "";
  const ariaLabel = state === "copied" ? "copied to clipboard" : label ? `${label} - ${LABEL}` : LABEL;
  const labelText = state === "copied" ? "copied" : label;

  return (
    <button
      type="button"
      className={["copy-button", className, statusClass].filter(Boolean).join(" ")}
      aria-label={ariaLabel}
      title={LABEL}
      onClick={copyEnvelope}
    >
      <svg
        className="copy-icon copy-icon--copy"
        viewBox="0 0 24 24"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="9" y="9" width="11" height="11" rx="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      </svg>
      <svg
        className="copy-icon copy-icon--check"
        viewBox="0 0 24 24"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M20 6 9 17l-5-5" />
      </svg>
      {labelText ? (
        <span className="copy-button__label">{labelText}</span>
      ) : (
        <span className="sr-only">{LABEL}</span>
      )}
    </button>
  );
}
