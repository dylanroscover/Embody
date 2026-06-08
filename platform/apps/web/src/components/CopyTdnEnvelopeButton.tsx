import { useRef, useState } from "react";
import { buildEmbodyEnvelope } from "../lib/tdnEnvelope";

const COPY_RESET_MS = 1500;
const LABEL = "Copy TDN for TouchDesigner";

interface Props {
  tdn: Record<string, unknown>;
  slug?: string;
  version?: number;
  className?: string;
}

export default function CopyTdnEnvelopeButton({
  tdn,
  slug,
  version,
  className = ""
}: Props) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);

  async function copyEnvelope() {
    if (timer.current !== undefined) {
      window.clearTimeout(timer.current);
    }

    try {
      const envelope = await buildEmbodyEnvelope(tdn, { slug, version });
      await navigator.clipboard.writeText(JSON.stringify(envelope));
      setState("copied");
      timer.current = window.setTimeout(() => setState("idle"), COPY_RESET_MS);
    } catch {
      setState("failed");
      timer.current = window.setTimeout(() => setState("idle"), COPY_RESET_MS);
    }
  }

  const statusClass = state === "copied" ? "is-copied" : state === "failed" ? "is-failed" : "";
  const ariaLabel = state === "copied" ? "Copied to clipboard" : LABEL;

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
      <span className="sr-only">{LABEL}</span>
    </button>
  );
}
