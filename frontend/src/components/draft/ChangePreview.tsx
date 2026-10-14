import { useState } from "react";
import { api } from "../../lib/api";
import type { DraftPreview, Lead } from "../../lib/types";

interface Props {
  leads: Lead[];
  proposal: { text: string; stages: string[] } | null;
}

// A real before/after: the model writes this lead's first email with the active
// changes, and again with the proposed one added. Nothing is saved or sent.
export default function ChangePreview({ leads, proposal }: Props) {
  const [leadId, setLeadId] = useState("");
  const [preview, setPreview] = useState<DraftPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const withCompany = leads.filter((l) => l.company);
  const chosen = leadId || withCompany[0]?.id || "";

  const run = async () => {
    if (!chosen) return;
    setLoading(true);
    setError(null);
    try {
      setPreview(await api.previewDraftChange(chosen, proposal && proposal.text.trim() ? proposal : undefined));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mdraft-card">
      <h2>Preview</h2>
      <label className="mdraft-field">
        <span>Preview as</span>
        <select value={chosen} onChange={(e) => { setLeadId(e.target.value); setPreview(null); }}>
          {withCompany.map((l) => (
            <option key={l.id} value={l.id}>
              {l.full_name} · {l.position ?? "—"}, {l.company}
            </option>
          ))}
        </select>
      </label>
      <div className="button-row">
        <button type="button" onClick={run} disabled={loading || !chosen}>
          {loading ? "Writing both versions…" : proposal ? "Preview with this change" : "Preview current instructions"}
        </button>
      </div>
      {error && <p className="error-text">{error}</p>}
      {preview?.simulated && (
        <p className="chatbot-hint">Mock mode: shows the fixed template. A real before/after needs the live OpenAI connection.</p>
      )}
      {preview && (
        <div className="mdraft-grid2">
          <div>
            <div className="chatbot-hint">Without the proposed change</div>
            <iframe title="Before" srcDoc={preview.before.html} className="mdraft-frame" />
          </div>
          <div>
            <div className="chatbot-hint">{proposal ? "With the proposed change" : "Current instructions"}</div>
            <iframe title="After" srcDoc={preview.after.html} className="mdraft-frame" />
          </div>
        </div>
      )}
      <p className="chatbot-hint">Each preview is two model calls, about a fifth of a cent. It reads the cached company research and never starts new research.</p>
    </div>
  );
}
