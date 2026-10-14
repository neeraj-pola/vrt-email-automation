import type { FlowStage } from "../../lib/types";
import { ALL, changesForStage, describeScope, type DraftChange } from "../../lib/draftChanges.ts";

interface Props {
  stages: FlowStage[];
  changes: DraftChange[];
  selected: string; // stage id, or "all"
  pendingStages: string[] | null; // stages a proposed (not yet applied) change would touch
  pendingText: string;
  onSelect: (stageId: string) => void;
}

// The first-email pipeline as a flowchart, one node per stage. Each node shows
// the built-in rule for that stage plus every active staff instruction aimed at it.
export default function FlowChart({ stages, changes, selected, pendingStages, pendingText, onSelect }: Props) {
  return (
    <div className="flow">
      {stages.map((st, i) => {
        const mine = changesForStage(changes, st.id, st.editable);
        const pending = st.editable && pendingStages !== null && (pendingStages.includes(ALL) || pendingStages.includes(st.id));
        const cls = ["flow__node", selected === st.id && "is-selected", pending && "is-pending", !st.editable && "is-locked"]
          .filter(Boolean)
          .join(" ");
        return (
          <div key={st.id}>
            <button type="button" className={cls} onClick={() => onSelect(st.id)} aria-disabled={!st.editable} aria-pressed={selected === st.id}>
              <span className="flow__head">
                <span className="flow__no">{st.no}</span>
                <span className="flow__title">{st.title}</span>
                {pending && <span className="mdraft-badge mdraft-badge--pending">Proposed change</span>}
                {mine.length > 0 && (
                  <span className="mdraft-badge mdraft-badge--red">
                    {mine.length} instruction{mine.length > 1 ? "s" : ""}
                  </span>
                )}
                {!st.editable && <span className="mdraft-badge">🔒 Required by law, not editable</span>}
              </span>
              <span className="flow__desc">{st.description}</span>
              {st.chips.length > 0 && (
                <span className="flow__chips">
                  {st.chips.map((c, j) => (
                    <span key={c}>
                      <span className="flow__chip">{c}</span>
                      {st.id === "research" && j < st.chips.length - 1 && <span className="flow__then"> →</span>}
                    </span>
                  ))}
                </span>
              )}
              {st.builtin_rule && (
                <span className="flow__rule">
                  <i>Built-in rule:</i> {st.builtin_rule}
                </span>
              )}
              {mine.map((c) => (
                <span key={c.id} className="flow__rule">
                  <i>
                    {c.name ? `${c.name} · ` : "Staff instruction · "}
                    {describeScope(c)}:
                  </i>{" "}
                  {c.text}
                </span>
              ))}
              {pending && pendingText.trim() && (
                <span className="flow__rule flow__rule--pending">
                  <i>Proposed:</i> {pendingText}
                </span>
              )}
            </button>
            {i < stages.length - 1 && <div className="flow__arrow" aria-hidden="true" />}
          </div>
        );
      })}
    </div>
  );
}
