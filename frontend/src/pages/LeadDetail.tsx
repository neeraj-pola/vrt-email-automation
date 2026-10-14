import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import type { Lead } from "../lib/types";
import LeadDetailContent from "../components/LeadDetailContent";

// Full-page variant of the lead detail -- used for direct/deep links
// (e.g. a URL shared with a teammate) and as the mobile fallback, where the
// dashboard's slide-over panel already goes full-bleed. Renders the same
// LeadDetailContent as the panel so the two never drift apart.
export default function LeadDetail() {
  const { id } = useParams<{ id: string }>();
  const [lead, setLead] = useState<Lead | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    api.getLead(id).then(setLead).catch((e) => setError(String(e)));
  }, [id]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!lead) return <div className="empty-state">Loading…</div>;

  return (
    <div className="lead-detail-page">
      <Link to="/" className="back-link">
        ← Back to dashboard
      </Link>
      <LeadDetailContent lead={lead} onUpdate={setLead} />
    </div>
  );
}
