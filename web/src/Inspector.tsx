import { useState, useEffect } from "react";
import { AppFrame } from "./AppFrame";
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";

/* ── Types ── */
interface InspectorProps {
  activeId: string | null;
  webUrl: string | null;
  /** DNS-free http://run-<id>.forge.localhost URL; preferred for the embed since
   *  it always resolves on the forge host even when the tunnel hostname won't. */
  localUrl: string | null;
}

type Tab = "preview" | "diff" | "verify";

/* ── Inspector ── */
export function Inspector({ activeId, webUrl, localUrl }: InspectorProps) {
  const [tab, setTab] = useState<Tab>("preview");
  const previewUrl = localUrl ?? webUrl;

  useEffect(() => {
    setTab(previewUrl ? "preview" : "diff");
  }, [activeId, previewUrl]);

  if (!activeId) {
    return (
      <div className="inspector">
        <div className="pane-placeholder">
          <div className="pane-placeholder-icon">⬚</div>
          <div className="pane-placeholder-label">select a session</div>
        </div>
      </div>
    );
  }

  const previewDisabled = !previewUrl;
  const TABS: { id: Tab; label: string; disabled?: boolean }[] = [
    { id: "preview", label: "preview", disabled: previewDisabled },
    { id: "diff", label: "diff" },
    { id: "verify", label: "verify" },
  ];

  return (
    <div className="inspector">
      <div className="inspector-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`inspector-tab${tab === t.id ? " is-active" : ""}${t.disabled ? " is-disabled" : ""}`}
            onClick={() => !t.disabled && setTab(t.id)}
            disabled={t.disabled}
            title={t.disabled ? "No web service available" : undefined}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="inspector-body">
        {tab === "preview" && <AppFrame webUrl={webUrl} localUrl={localUrl} />}
        {tab === "diff" && <DiffView activeId={activeId} />}
        {tab === "verify" && <VerifyView activeId={activeId} />}
      </div>
    </div>
  );
}
