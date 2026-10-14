import type { FlowStage } from "../../lib/types";
import { ALL } from "../../lib/draftChanges.ts";

export interface ComposerState {
  stage: string; // "all" or a stage id
  scope: "all" | "one";
  company: string;
  text: string;
}

const EXAMPLES = [
  "Make this part shorter",
  "Mention the free 7 Stages assessment",
  "Sound more casual and friendly",
  "End with a question instead",
  "Offer evening times for the call",
];

interface Props {
  stages: FlowStage[];
  companies: string[];
  value: ComposerState;
  onChange: (next: ComposerState) => void;
  onPropose: () => void;
}

// Step 1: where in the flow, for whom, and what to change in plain words.
export default function ChangeComposer({ stages, companies, value, onChange, onPropose }: Props) {
  const editable = stages.filter((s) => s.editable);
  return (
    <div className="mdraft-card">
      <span className="mdraft-step">Step 1 · Say what should change</span>
      <h2>Suggestion for the draft</h2>

      <div className="mdraft-field">
        <span>Where in the flow?</span>
        <div className="mdraft-seg">
          <button type="button" className={value.stage === ALL ? "is-on" : ""} onClick={() => onChange({ ...value, stage: ALL })}>
            Entire email
          </button>
          {editable.map((s) => (
            <button key={s.id} type="button" className={value.stage === s.id ? "is-on" : ""} onClick={() => onChange({ ...value, stage: s.id })}>
              {s.no} {s.title}
            </button>
          ))}
        </div>
      </div>

      <div className="mdraft-field">
        <span>For whom?</span>
        <div className="mdraft-seg">
          <button type="button" className={value.scope === "all" ? "is-on" : ""} onClick={() => onChange({ ...value, scope: "all" })}>
            All companies
          </button>
          <button
            type="button"
            className={value.scope === "one" ? "is-on" : ""}
            onClick={() => onChange({ ...value, scope: "one", company: value.company || companies[0] || "" })}
            disabled={companies.length === 0}
          >
            One company
          </button>
        </div>
      </div>

      {value.scope === "one" && (
        <label className="mdraft-field">
          <span>Company</span>
          <select value={value.company} onChange={(e) => onChange({ ...value, company: e.target.value })}>
            {companies.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
      )}

      <label className="mdraft-field">
        <span>Your suggestion, in plain words</span>
        <textarea
          value={value.text}
          maxLength={500}
          onChange={(e) => onChange({ ...value, text: e.target.value })}
          placeholder="e.g. Keep it shorter and mention the free 7 Stages assessment"
        />
      </label>

      <div className="mdraft-chips">
        <span className="chatbot-hint">Try:</span>
        {EXAMPLES.map((ex) => (
          <button key={ex} type="button" onClick={() => onChange({ ...value, text: ex })}>
            {ex}
          </button>
        ))}
      </div>

      <div className="button-row">
        <button type="button" className="primary" onClick={onPropose} disabled={!value.text.trim()}>
          Propose change
        </button>
      </div>
    </div>
  );
}
