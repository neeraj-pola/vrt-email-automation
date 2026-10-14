import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { CompanyProfile, Lead } from "../lib/types";

const FOUND_VIA: Record<string, string> = { email_domain: "their email domain", search: "a web search" };
const STAGE: Record<string, string> = { starting: "Starting out", growing: "Growing", established: "Established", unknown: "Stage unclear" };

// What we know about this lead's company, and which of VRT's 27 challenges
// the company's own pages give evidence for. Researched once per company
// (shared by every lead there) when a draft is generated; Refresh re-runs it.
export default function CompanyResearch({ lead }: { lead: Lead }) {
  const [profile, setProfile] = useState<CompanyProfile | null | undefined>(undefined); // undefined = loading
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setProfile(undefined);
    setError(null);
    api.getCompanyProfile(lead.id).then(setProfile).catch(() => setProfile(null));
  }, [lead.id]);

  if (!lead.company) return null;

  const refresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      setProfile(await api.refreshCompanyProfile(lead.id));
    } catch (e) {
      setError(String(e));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="qualbox">
      <h3>
        Company research
        {profile?.status === "ok" && <span className="locked-badge">{STAGE[profile.stage_hint]}</span>}
      </h3>

      {profile === undefined && <p className="chatbot-hint">Loading…</p>}

      {profile === null && (
        <p className="chatbot-hint">Not researched yet. It runs automatically when a draft is generated for this lead.</p>
      )}

      {profile?.status === "not_found" && (
        <p className="chatbot-hint">
          Couldn't find a website to read for {lead.company}, so the draft is based on the person's role and company size
          only.
        </p>
      )}

      {profile?.status === "ok" && (
        <>
          <p className="reasoning">{profile.summary}</p>
          {profile.source_url && (
            <p className="chatbot-hint" style={{ marginTop: -4 }}>
              Source:{" "}
              <a href={profile.source_url} target="_blank" rel="noreferrer">
                {profile.source_url}
              </a>
              {profile.found_via && ` (found via ${FOUND_VIA[profile.found_via]})`}
            </p>
          )}
          {profile.challenges.length > 0 ? (
            <ul className="challenge-list">
              {profile.challenges.map((c) => (
                <li key={c.id}>
                  <strong>
                    #{c.id} {c.name}
                  </strong>
                  <span>{c.evidence}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="chatbot-hint">No clear sign of any of the 27 challenges on their site, so none is assumed.</p>
          )}
        </>
      )}

      {error && <p className="error-text">{error}</p>}
      <div className="button-row">
        <button onClick={refresh} disabled={refreshing}>
          {refreshing ? "Researching…" : profile ? "Refresh research" : "Research now"}
        </button>
      </div>
    </div>
  );
}
