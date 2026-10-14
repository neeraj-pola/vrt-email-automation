import { useEffect } from "react";
import type { Lead } from "../lib/types";
import LeadDetailContent from "./LeadDetailContent";
import { IconClose } from "./icons";

interface Props {
  lead: Lead;
  onClose: () => void;
  onUpdate: (lead: Lead) => void;
}

// Attio-style slide-over: overlays the dashboard on desktop, goes full-bleed
// on mobile via the .panel CSS breakpoint (see index.css).
export default function LeadDetailPanel({ lead, onClose, onUpdate }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <div className="panel-backdrop" onClick={onClose} />
      <div className="panel" role="dialog" aria-label={`${lead.full_name} details`}>
        <button className="panel__close" onClick={onClose} aria-label="Close">
          <IconClose />
        </button>
        <LeadDetailContent lead={lead} onUpdate={onUpdate} />
      </div>
    </>
  );
}
