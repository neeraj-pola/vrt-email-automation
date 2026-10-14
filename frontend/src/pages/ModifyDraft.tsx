import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { FlowStage, Lead } from "../lib/types";
import { companyOptions } from "../lib/filters";
import {
  ALL,
  applyProposal,
  commit,
  deleteSaved,
  emptyProposal,
  isInitial,
  lastStep,
  newSession,
  proposalFromSaved,
  removeChange,
  resetAll,
  toggleChange,
  undo,
  type DraftContent,
  type Proposal,
  type Session,
} from "../lib/draftChanges.ts";
import Sidebar from "../components/Sidebar";
import FlowChart from "../components/draft/FlowChart";
import ChangeComposer, { type ComposerState } from "../components/draft/ChangeComposer";
import ChangeReview from "../components/draft/ChangeReview";
import ChangePreview from "../components/draft/ChangePreview";
import ChangeLists from "../components/draft/ChangeLists";

const NO_COMPOSER: ComposerState = { stage: ALL, scope: "all", company: "", text: "" };

// Modify draft: the flow of the first email, with staff changes to it. A
// change is plain words aimed at the whole email or at one stage, for all
// companies or some. Everything is saved on the server as one versioned
// document, which is what makes Undo and "Back to initial state" exact.
export default function ModifyDraft() {
  const navigate = useNavigate();
  const [stages, setStages] = useState<FlowStage[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [session, setSession] = useState<Session>(newSession());
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [composer, setComposer] = useState<ComposerState>(NO_COMPOSER);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [regenerate, setRegenerate] = useState(true);
  const [confirmReset, setConfirmReset] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [flow, state, allLeads] = await Promise.all([api.getDraftFlow(), api.getDraftChanges(), api.listLeads()]);
      setStages(flow);
      setLeads(allLeads);
      setVersion(state.version);
      setSession(newSession({ changes: state.changes, saved: state.saved }));
      setProposal(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const companies = useMemo(() => companyOptions(leads).filter((c) => c.key !== "__none__"), [leads]);
  const companyNames = useMemo(() => companies.map((c) => c.name), [companies]);
  const content = session.content;
  const step = lastStep(session);

  // Saves `next` to the server; only a successful save changes what the page shows.
  const save = async (next: DraftContent): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      const saved = await api.saveDraftChanges(version, next);
      setVersion(saved.version);
      return true;
    } catch (e) {
      const msg = String(e);
      setError(msg.includes("409") ? "Someone else changed these instructions since you opened the page. Reload to see their changes." : msg);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const change = async (label: string, next: DraftContent): Promise<boolean> => {
    if (!(await save(next))) return false;
    setSession((s) => commit(s, label, next));
    return true;
  };

  const handlePropose = () => {
    const p = emptyProposal();
    p.text = composer.text.trim();
    p.stages = [composer.stage];
    p.companies = composer.scope === "one" && composer.company ? [composer.company] : null;
    setProposal(p);
    setNotice(null);
  };

  const handleApply = async () => {
    if (!proposal) return;
    const r = applyProposal(content, proposal, () => crypto.randomUUID(), new Date().toISOString());
    if (!(await change(r.label, r.content))) return;
    let message = "Applied. It affects drafts written from now on.";
    if (regenerate) {
      try {
        const out = await api.regenerateDrafts(proposal.companies);
        message = `Applied. Rewrote ${out.regenerated} unsent draft${out.regenerated === 1 ? "" : "s"}.`;
      } catch (e) {
        message = `Applied, but rewriting the unsent drafts failed: ${e}`;
      }
    }
    setProposal(null);
    setComposer(NO_COMPOSER);
    setNotice(message);
  };

  const handleUndo = async () => {
    const prev = undo(session);
    const target = lastStep(session);
    if (!prev || !target) return;
    if (!(await save(target.before))) return;
    setSession(prev);
    setProposal(null);
    setConfirmReset(false);
    setNotice(`Undid: ${target.label}`);
  };

  const handleReset = async () => {
    const next = resetAll(session);
    if (!(await save(next.content))) return;
    setSession(next);
    setProposal(null);
    setComposer(NO_COMPOSER);
    setConfirmReset(false);
    setNotice("Back to the initial state. Undo brings your changes back.");
  };

  const apply = (r: { content: DraftContent; label: string }) => change(r.label, r.content);

  const handleReuse = (name: string) => {
    const saved = content.saved.find((s) => s.name === name);
    if (!saved) return;
    setProposal(proposalFromSaved(content, saved, companyNames));
    setNotice(`Pick the companies to reuse "${name}" on.`);
  };

  const selectStage = (id: string) => {
    const st = stages.find((s) => s.id === id);
    if (!st?.editable) {
      setNotice("The sign-off, links, address and unsubscribe link are required by law, so they can't be changed.");
      return;
    }
    setComposer((c) => ({ ...c, stage: c.stage === id ? ALL : id }));
  };

  return (
    <div className="shell">
      <Sidebar leads={leads} active="all" onSelect={() => navigate("/")} />
      <div className="mobile-topbar">
        <div className="mobile-topbar__title">
          <img src="/logo.png" alt="VRT Management Group" className="mobile-topbar__logo" />
        </div>
      </div>
      <Link to="/" className="back-link mobile-only-back">
        ← Back to leads
      </Link>

      <div className="main">
        <div className="topbar">
          <h1>Modify draft</h1>
          <div className="spacer" />
          <button type="button" onClick={handleUndo} disabled={!step || busy} title={step ? `Undo: ${step.label}` : "Nothing to undo"}>
            ↶ Undo{step ? ` · ${step.label.length > 26 ? `${step.label.slice(0, 25)}…` : step.label}` : ""}
          </button>
          <button type="button" onClick={() => setConfirmReset(true)} disabled={isInitial(content) || busy}>
            Back to initial state
          </button>
        </div>

        <div className="mdraft">
          <p className="chatbot-hint mdraft__intro">
            The flow of the first email, from lead data to the links we attach. Pick a stage, say what should change in plain words, check how the draft
            changes, then choose which stages and companies it applies to.
          </p>

          {loading && <div className="empty-state">Loading…</div>}
          {error && (
            <div className="error-banner mdraft__banner">
              {error}{" "}
              <button type="button" onClick={load}>
                Reload
              </button>
            </div>
          )}
          {notice && !error && <div className="busy-banner mdraft__banner">{notice}</div>}
          {confirmReset && (
            <div className="error-banner mdraft__banner" role="alert">
              Remove all {content.changes.length} change{content.changes.length === 1 ? "" : "s"} and {content.saved.length} saved change
              {content.saved.length === 1 ? "" : "s"} and go back to the starting point? You can still undo it.{" "}
              <button type="button" className="primary" onClick={handleReset} disabled={busy}>
                Yes, go back
              </button>{" "}
              <button type="button" onClick={() => setConfirmReset(false)}>
                Keep my changes
              </button>
            </div>
          )}

          {!loading && stages.length > 0 && (
            <div className="mdraft__cols">
              <section className="mdraft-card" aria-label="First email flow">
                <h2>First email flow</h2>
                <p className="chatbot-hint">Click a stage to aim your suggestion at it.</p>
                <FlowChart
                  stages={stages}
                  changes={content.changes}
                  selected={composer.stage}
                  pendingStages={proposal ? proposal.stages : null}
                  pendingText={proposal?.text ?? ""}
                  onSelect={selectStage}
                />
              </section>

              <div className="mdraft__side">
                <ChangeComposer stages={stages} companies={companyNames} value={composer} onChange={setComposer} onPropose={handlePropose} />
                {proposal && (
                  <ChangeReview
                    stages={stages}
                    companies={companies}
                    leads={leads}
                    proposal={proposal}
                    regenerate={regenerate}
                    busy={busy}
                    onChange={setProposal}
                    onRegenerateChange={setRegenerate}
                    onApply={handleApply}
                    onDiscard={() => setProposal(null)}
                  />
                )}
                <ChangePreview leads={leads} proposal={proposal ? { text: proposal.text, stages: proposal.stages } : null} />
                <ChangeLists
                  stages={stages}
                  content={content}
                  allCompanies={companyNames}
                  busy={busy}
                  onToggle={(id) => apply(toggleChange(content, id))}
                  onRemove={(id) => apply(removeChange(content, id))}
                  onReuse={handleReuse}
                  onDeleteSaved={(name) => apply(deleteSaved(content, name))}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
