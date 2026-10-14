import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { Lead } from "../lib/types";
import Sidebar from "../components/Sidebar";
import ClickActivityChart from "../components/ClickActivityChart";
import { initials } from "../lib/filters";

// Explicit company timezone (Botsford, CT), not the viewer's browser
// default -- see ClickActivityChart.tsx's COMPANY_TZ for the bug this
// avoids: without it, a staff member viewing from outside Eastern time
// would see a different (wrong) hour/day than what's on the chart, for
// the exact same click.
const COMPANY_TZ = "America/New_York";

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: COMPANY_TZ,
  });
}

interface ClickRowProps {
  lead: Lead;
  timestamp: string;
}

function ClickRow({ lead, timestamp }: ClickRowProps) {
  return (
    <Link to={`/leads/${lead.id}`} className="click-row">
      <div className="avatar">{initials(lead.full_name)}</div>
      <div className="click-row__meta">
        <span className="lead-name">{lead.full_name}</span>
        <div className="lead-email">{lead.email}</div>
      </div>
      <span className="click-row__time">{formatDateTime(timestamp)}</span>
    </Link>
  );
}

function ClickSection({ title, rows }: { title: string; rows: ClickRowProps[] }) {
  return (
    <div className="qualbox click-section">
      <h3>
        {title} <span className="locked-badge">{rows.length}</span>
      </h3>
      {rows.length === 0 ? (
        <p className="chatbot-hint">No leads yet.</p>
      ) : (
        <div className="click-list">
          {rows.map((r) => (
            <ClickRow key={r.lead.id} lead={r.lead} timestamp={r.timestamp} />
          ))}
        </div>
      )}
    </div>
  );
}

// Every link in an outbound email is routed through /public/click (backend)
// so a real click -- unlike an open-pixel "opened" signal, which Apple
// Mail's Privacy Protection makes meaningless by pre-fetching every image
// regardless of whether a human ever opens the email -- is a trustworthy
// engagement signal. This page is just a read of that same lead data,
// grouped by which link(s) each lead actually clicked.
export default function Analytics() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    api
      .listLeads()
      .then(setLeads)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const websiteClicks = leads
    .filter((l) => l.website_clicked_at)
    .map((l) => ({ lead: l, timestamp: l.website_clicked_at! }))
    .sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));

  const getStartedClicks = leads
    .filter((l) => l.get_started_clicked_at)
    .map((l) => ({ lead: l, timestamp: l.get_started_clicked_at! }))
    .sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));

  const bothClicks = leads
    .filter((l) => l.website_clicked_at && l.get_started_clicked_at)
    .map((l) => ({
      lead: l,
      // Whichever happened more recently, so the list reads "most recently active" first.
      timestamp: l.website_clicked_at! > l.get_started_clicked_at! ? l.website_clicked_at! : l.get_started_clicked_at!,
    }))
    .sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));

  return (
    <div className="shell">
      <Sidebar leads={leads} active="all" onSelect={() => navigate("/")} />
      <div className="mobile-topbar">
        <div className="mobile-topbar__title"><img src="/logo.png" alt="VRT Management Group" className="mobile-topbar__logo" /></div>
      </div>
      <Link to="/" className="back-link mobile-only-back">
        ← Back to leads
      </Link>

      <div className="main">
        <div className="topbar">
          <h1>Analytics</h1>
        </div>

        {loading && <div className="empty-state">Loading…</div>}
        {error && <div className="error-banner">{error}</div>}

        {!loading && !error && (
          <div className="analytics-page">
            <p className="chatbot-hint" style={{ marginBottom: 22 }}>
              Every link in an outbound email is tracked as a real click (not an unreliable open pixel) — grouped
              here by which one each lead actually followed. Click a name to open their lead panel.
            </p>
            <ClickActivityChart leads={leads} />
            <div className="analytics-grid">
              <ClickSection title="Clicked website link" rows={websiteClicks} />
              <ClickSection title="Clicked Get started" rows={getStartedClicks} />
              <ClickSection title="Clicked both" rows={bothClicks} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
