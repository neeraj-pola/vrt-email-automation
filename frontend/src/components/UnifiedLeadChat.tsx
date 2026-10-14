import { useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type { Lead } from "../lib/types";

interface Props {
  lead: Lead;
  onDraftUpdated: (lead: Lead) => void;
}

type Mode = "edit" | "ask";

interface ChatEntry {
  mode: Mode;
  role: "staff" | "assistant";
  text: string;
}

// One chat window for both jobs -- rewriting the draft, and asking "what
// should we say if..." -- kept as two explicit actions rather than one
// input where the AI guesses your intent, so it can never silently edit
// the draft when you only meant to ask a question (or vice versa). Stays
// mounted permanently once a lead is open, so the conversation is never
// lost, regardless of what else is happening in the panel.
export default function UnifiedLeadChat({ lead, onDraftUpdated }: Props) {
  const [history, setHistory] = useState<ChatEntry[]>([]);
  const [mode, setMode] = useState<Mode>(lead.current_draft_subject ? "edit" : "ask");
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);

  const canEdit = Boolean(lead.current_draft_subject) && !lead.draft_locked;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!input.trim() || sending) return;
    const text = input.trim();
    setHistory((h) => [...h, { mode, role: "staff", text }]);
    setInput("");
    setSending(true);
    try {
      if (mode === "edit") {
        const updated = await api.editDraftViaChatbot(lead.id, text);
        onDraftUpdated(updated);
        setHistory((h) => [...h, { mode, role: "assistant", text: "Draft updated — see the editor above." }]);
      } else {
        const priorAskTurns = history
          .filter((entry) => entry.mode === "ask")
          .map((entry) => ({ role: entry.role, text: entry.text }));
        const { answer } = await api.askAboutLead(lead.id, text, priorAskTurns);
        setHistory((h) => [...h, { mode, role: "assistant", text: answer }]);
      }
    } catch (err) {
      setHistory((h) => [...h, { mode, role: "assistant", text: `Failed: ${err}` }]);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="qualbox">
      <h3>Chat about this lead</h3>
      <div className="button-row" style={{ marginBottom: 10 }}>
        <button
          type="button"
          className={mode === "edit" ? "primary" : ""}
          onClick={() => setMode("edit")}
          disabled={!canEdit}
          title={canEdit ? "" : "No editable draft right now"}
        >
          Edit draft
        </button>
        <button type="button" className={mode === "ask" ? "primary" : ""} onClick={() => setMode("ask")}>
          Just ask
        </button>
      </div>
      <p className="chatbot-hint">
        {mode === "edit"
          ? 'e.g. "Make it shorter" or "Lead with the VEGA cohort angle instead" — rewrites the current draft.'
          : "e.g. \"What if they ask about pricing?\" — talking points only, never edits or sends anything."}
      </p>
      <div className="chatbot-history">
        {history.map((entry, i) => (
          <div key={i} className={`chatbot-entry chatbot-entry-${entry.role === "staff" ? "staff" : "system"}`}>
            {entry.mode === "edit" && entry.role === "staff" && <strong>Edit: </strong>}
            {entry.text}
          </div>
        ))}
      </div>
      <form onSubmit={handleSubmit} className="chatbot-form">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={mode === "edit" ? "Describe the change you want…" : "Ask a question…"}
          disabled={sending || (mode === "edit" && !canEdit)}
        />
        <button type="submit" disabled={sending || !input.trim() || (mode === "edit" && !canEdit)}>
          {sending ? "Working…" : "Send"}
        </button>
      </form>
    </div>
  );
}
