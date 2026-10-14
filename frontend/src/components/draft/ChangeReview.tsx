import { useMemo, useState } from "react";
import type { FlowStage } from "../../lib/types";
import { ALL, canApply, normalizeStages, unsentInScope, warningFor, type Proposal, type UnsentLead } from "../../lib/draftChanges.ts";

export interface CompanyOption {
  name: string;
  count: number;
}

interface Props {
  stages: FlowStage[];
  companies: CompanyOption[];
  leads: UnsentLead[];
  proposal: Proposal;
  regenerate: boolean;
  busy: boolean;
  onChange: (next: Proposal) => void;
  onRegenerateChange: (v: boolean) => void;
  onApply: () => void;
  onDiscard: () => void;
}

// Steps 2 and 3: check the wording that will go into the prompt, then choose
// which stages and companies it applies to (and optionally name it for reuse).
export default function ChangeReview({ stages, companies, leads, proposal, regenerate, busy, onChange, onRegenerateChange, onApply, onDiscard }: Props) {
  const [filter, setFilter] = useState("");
  const editable = stages.filter((s) => s.editable);
  const warning = warningFor(proposal.text);
  const unsent = unsentInScope(leads, proposal.companies);
  const shown = useMemo(() => companies.filter((c) => c.name.toLowerCase().includes(filter.trim().toLowerCase())), [companies, filter]);

  const toggleStage = (id: string, on: boolean) => {
    if (id === ALL) return onChange({ ...proposal, stages: on ? [ALL] : [] });
    const rest = proposal.stages.filter((s) => s !== ALL && s !== id);
    onChange({ ...proposal, stages: normalizeStages(on ? [...rest, id] : rest) });
  };

  const toggleCompany = (name: string, on: boolean) => {
    const current = proposal.companies ?? [];
    const next = on ? [...current, name] : current.filter((c) => c !== name);
    onChange({ ...proposal, companies: next.length > 0 ? next : null }); // nothing ticked falls back to everyone
  };

  return (
    <>
      <div className="mdraft-card">
        <span className="mdraft-step">Step 2 · Check what changes</span>
        <h2>Instruction that will be added to the prompt</h2>
        {warning && <div className="mdraft-warn">⚠ {warning}</div>}
        <label className="mdraft-field">
          <span>Edit the wording before it is embedded</span>
          <textarea value={proposal.text} maxLength={500} onChange={(e) => onChange({ ...proposal, text: e.target.value })} />
        </label>
        <p className="chatbot-hint">
          The flowchart on the left shows this as a dashed "Proposed change". Use Preview below to see a real before and after for one lead.
        </p>
      </div>

      <div className="mdraft-card">
        <span className="mdraft-step">Step 3 · Choose where it applies</span>
        <h2>Apply this change to</h2>
        <div className="mdraft-grid2">
          <div>
            <div className="chatbot-hint">Stages</div>
            <div className="mdraft-list">
              <label className="mdraft-check">
                <input type="checkbox" checked={proposal.stages.includes(ALL)} onChange={(e) => toggleStage(ALL, e.target.checked)} /> Entire email
              </label>
              {editable.map((s) => (
                <label key={s.id} className="mdraft-check">
                  <input type="checkbox" checked={proposal.stages.includes(s.id)} onChange={(e) => toggleStage(s.id, e.target.checked)} /> {s.no} {s.title}
                </label>
              ))}
            </div>
          </div>
          <div>
            <div className="chatbot-hint">Companies</div>
            <div className="mdraft-list">
              <label className="mdraft-check">
                <input type="checkbox" checked={proposal.companies === null} onChange={(e) => onChange({ ...proposal, companies: e.target.checked ? null : [] })} /> All companies
              </label>
              {companies.length > 8 && (
                <input type="text" placeholder="Find a company…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Find a company" />
              )}
              <div className="mdraft-scroll">
                {shown.map((c) => (
                  <label key={c.name} className="mdraft-check">
                    <input
                      type="checkbox"
                      checked={proposal.companies !== null && proposal.companies.includes(c.name)}
                      onChange={(e) => toggleCompany(c.name, e.target.checked)}
                    />{" "}
                    {c.name} <span className="chatbot-hint">({c.count} lead{c.count > 1 ? "s" : ""})</span>
                  </label>
                ))}
              </div>
            </div>
          </div>
        </div>

        <label className="mdraft-field">
          <span>Name this change (optional)</span>
          <input
            type="text"
            maxLength={60}
            placeholder="e.g. Short intro with assessment"
            value={proposal.name}
            onChange={(e) => onChange({ ...proposal, name: e.target.value })}
          />
        </label>
        <p className="chatbot-hint" style={{ marginTop: -6 }}>
          {proposal.reuseOf
            ? "Reusing a saved change. Keep the name to leave it in Saved changes."
            : "A named change is saved so you can reuse it on other companies with one click. Unnamed changes apply once and can't be reused."}
        </p>

        <label className="mdraft-check">
          <input type="checkbox" checked={regenerate} onChange={(e) => onRegenerateChange(e.target.checked)} /> Also rewrite the <strong>{unsent}</strong> unsent draft
          {unsent === 1 ? "" : "s"} in scope (sent emails are never touched)
        </label>

        <div className="button-row">
          <button type="button" className="primary" onClick={onApply} disabled={!canApply(proposal) || busy}>
            {busy ? "Saving…" : "Apply change"}
          </button>
          <button type="button" onClick={onDiscard} disabled={busy}>
            Discard
          </button>
        </div>
      </div>
    </>
  );
}
