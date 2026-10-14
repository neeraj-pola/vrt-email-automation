import type { FlowStage } from "../../lib/types";
import { ALL, coveredBy, describeScope, type DraftContent } from "../../lib/draftChanges.ts";

interface Props {
  stages: FlowStage[];
  content: DraftContent;
  allCompanies: string[];
  busy: boolean;
  onToggle: (id: string) => void;
  onRemove: (id: string) => void;
  onReuse: (name: string) => void;
  onDeleteSaved: (name: string) => void;
}

export default function ChangeLists({ stages, content, allCompanies, busy, onToggle, onRemove, onReuse, onDeleteSaved }: Props) {
  const stageTitle = (id: string) => (id === ALL ? "Entire email" : stages.find((s) => s.id === id)?.title ?? id);
  const stageList = (ids: string[]) => ids.map(stageTitle).join(", ");

  return (
    <>
      <div className="mdraft-card">
        <h2>Active instructions</h2>
        {content.changes.length === 0 && <p className="chatbot-hint">None yet. Propose a change above.</p>}
        {content.changes.map((c) => (
          <div key={c.id} className={`mdraft-item ${c.enabled ? "" : "is-off"}`}>
            <div>{c.text}</div>
            <div className="mdraft-item__meta">
              {c.name && <span className="mdraft-badge mdraft-badge--name">{c.name}</span>}
              <span className="mdraft-badge">{stageList(c.stages)}</span>
              <span className="mdraft-badge">{describeScope(c)}</span>
              <button type="button" onClick={() => onToggle(c.id)} disabled={busy}>
                {c.enabled ? "Turn off" : "Turn on"}
              </button>
              <button type="button" onClick={() => onRemove(c.id)} disabled={busy}>
                Remove
              </button>
            </div>
          </div>
        ))}
        <p className="chatbot-hint">
          Instructions are added to the first-email prompt in this order, after the built-in rules, and never override them. Turning one off changes only drafts
          written from now on.
        </p>
      </div>

      <div className="mdraft-card">
        <h2>Saved changes</h2>
        {content.saved.length === 0 && (
          <p className="chatbot-hint">None yet. Give a change a name when you apply it, and it appears here so you can reuse it on other companies in one click.</p>
        )}
        {content.saved.map((s) => {
          const covered = coveredBy(content, s.name, allCompanies).size;
          return (
            <div key={s.name} className="mdraft-item">
              <div>
                <strong>{s.name}</strong> · {s.text}
              </div>
              <div className="mdraft-item__meta">
                <span className="mdraft-badge">{stageList(s.stages)}</span>
                <span className="mdraft-badge">
                  Used on {allCompanies.length > 0 && covered === allCompanies.length ? "all companies" : `${covered} of ${allCompanies.length} companies`}
                </span>
                <button type="button" onClick={() => onReuse(s.name)} disabled={busy}>
                  Reuse on other companies
                </button>
                <button type="button" onClick={() => onDeleteSaved(s.name)} disabled={busy}>
                  Delete
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
