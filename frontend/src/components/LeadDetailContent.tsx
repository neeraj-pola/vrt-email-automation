import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { DraftVersion, Lead, Schedule } from "../lib/types";
import { deriveStage, nextFollowUpDueAt } from "../lib/stage";
import { emailStatusLabel, qualifiedStatusLabel } from "../lib/filters";
import { getPipelineConfig, getSchedule, type PipelineConfig } from "../lib/config";
import { htmlToPlainText, plainTextToHtml } from "../lib/htmlText";
import UnifiedLeadChat from "./UnifiedLeadChat";
import CompanyResearch from "./CompanyResearch";

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

interface Props {
  lead: Lead;
  onUpdate: (lead: Lead) => void;
}

function versionLabel(version: DraftVersion): string {
  const when = formatDateTime(version.created_at);
  const label = version.stage || (version.source === "chatbot" ? "Chatbot rewrite" : version.source === "manual" ? "Manual edit" : "Initial draft");
  return `${label}${version.sent ? "" : " (unsent)"} — ${when}`;
}

// Shared detail content -- rendered inside the desktop slide-over panel
// (LeadDetailPanel) and the full-page mobile/deep-link route (LeadDetail).
//
// Linear flow, kept deliberately simple: the "Current draft" editor always
// shows the live draft -- nothing ever swaps it out for a historical
// version. Previous versions are a separate, plain list below (newest
// first); expanding one shows a preview, and a click restores it. The
// chatbot (draft edits + lead Q&A) is one always-visible panel, mounted
// permanently so its conversation is never lost.
export default function LeadDetailContent({ lead, onUpdate }: Props) {
  const [subject, setSubject] = useState(lead.current_draft_subject ?? "");
  const [plainBody, setPlainBody] = useState(htmlToPlainText(lead.current_draft_body_html ?? ""));
  const [savedSubject, setSavedSubject] = useState(lead.current_draft_subject ?? "");
  const [savedPlainBody, setSavedPlainBody] = useState(htmlToPlainText(lead.current_draft_body_html ?? ""));
  const [notes, setNotes] = useState(lead.notes ?? "");
  const [crmStatus, setCrmStatus] = useState(lead.crm_status ?? "");
  const [saleStatus, setSaleStatus] = useState(lead.sale_status ?? "");
  const [fieldsSaved, setFieldsSaved] = useState(false);
  const [callTime, setCallTime] = useState("");
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<{ subject: string; html: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [pipelineConfig, setPipelineConfig] = useState<PipelineConfig | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [expandedVersion, setExpandedVersion] = useState<number | null>(null);
  const [versionPreviews, setVersionPreviews] = useState<Record<number, { subject: string; html: string }>>({});

  useEffect(() => {
    getPipelineConfig().then(setPipelineConfig).catch(() => setPipelineConfig(null));
    getSchedule().then(setSchedule).catch(() => setSchedule(null));
  }, []);

  useEffect(() => {
    setNotes(lead.notes ?? "");
    setCrmStatus(lead.crm_status ?? "");
    setSaleStatus(lead.sale_status ?? "");
    setCallTime("");
    setExpandedVersion(null);
    setVersionPreviews({});
  }, [lead.id]);

  // Fires whenever the *saved* draft actually changes -- switching leads,
  // a chatbot rewrite, a restore, a fresh send -- so the editor never sits
  // there showing stale content after something else updated it.
  useEffect(() => {
    const plain = htmlToPlainText(lead.current_draft_body_html ?? "");
    setSubject(lead.current_draft_subject ?? "");
    setPlainBody(plain);
    setSavedSubject(lead.current_draft_subject ?? "");
    setSavedPlainBody(plain);
  }, [lead.id, lead.current_draft_subject, lead.current_draft_body_html]);

  // Live "what the lead will actually receive" preview of the CURRENT
  // draft only -- historical versions get their own preview on expand.
  useEffect(() => {
    if (!lead.current_draft_subject || !lead.current_draft_body_html) {
      setPreview(null);
      return;
    }
    setPreviewLoading(true);
    api
      .previewDraft(lead.id)
      .then(setPreview)
      .catch(() => setPreview(null))
      .finally(() => setPreviewLoading(false));
  }, [lead.id, lead.current_draft_subject, lead.current_draft_body_html]);

  const isDirty = subject !== savedSubject || plainBody !== savedPlainBody;

  const handleGenerateDraft = async () => {
    setSaving(true);
    try {
      const [updated] = await api.generateDrafts([lead.id]);
      onUpdate(updated);
    } catch (e) {
      alert(`Draft generation failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleSaveDraft = async () => {
    if (!isDirty) return;
    setSaving(true);
    try {
      onUpdate(await api.editDraftManually(lead.id, subject, plainTextToHtml(plainBody)));
    } catch (e) {
      alert(`Save failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleSend = async () => {
    if (!confirm(`Send this email to ${lead.full_name} (${lead.email})?`)) return;
    setSaving(true);
    try {
      const result = await api.sendBatch([lead.id]);
      if (result.sent.includes(lead.id)) {
        onUpdate(await api.getLead(lead.id));
      } else {
        alert(`Not sent -- ${JSON.stringify(result.errors)}`);
      }
    } catch (e) {
      alert(`Send failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleSaveManualFields = async () => {
    setSaving(true);
    try {
      onUpdate(await api.updateManualFields(lead.id, { notes, crm_status: crmStatus, sale_status: saleStatus }));
      setFieldsSaved(true);
      window.setTimeout(() => setFieldsSaved(false), 2500);
    } catch (e) {
      alert(`Save failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleQualifyOverride = async (status: "qualified" | "not_qualified") => {
    setSaving(true);
    try {
      onUpdate(await api.overrideQualifiedStatus(lead.id, status));
    } catch (e) {
      alert(`Update failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleConfirmCall = async () => {
    if (!callTime) return;
    setSaving(true);
    try {
      onUpdate(await api.confirmCall(lead.id, new Date(callTime).toISOString()));
      alert("Call confirmed and confirmation email sent to the lead.");
    } catch (e) {
      alert(`Confirm failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleUnsubscribe = async () => {
    if (!confirm(`Mark ${lead.full_name} as unsubscribed? No further emails will ever be sent to them.`)) return;
    setSaving(true);
    try {
      onUpdate(await api.unsubscribeLead(lead.id));
    } catch (e) {
      alert(`Failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const toggleVersion = (index: number) => {
    if (expandedVersion === index) {
      setExpandedVersion(null);
      return;
    }
    setExpandedVersion(index);
    if (!versionPreviews[index]) {
      const v = lead.draft_history[index];
      api
        .previewDraft(lead.id, v.subject, v.body_html)
        .then((p) => setVersionPreviews((prev) => ({ ...prev, [index]: p })))
        .catch(() => {});
    }
  };

  const handleRestoreVersion = async (index: number) => {
    if (!confirm("Restore this version as the current draft? The current draft will be kept in history first.")) return;
    setSaving(true);
    try {
      onUpdate(await api.restoreDraftVersion(lead.id, index));
      setExpandedVersion(null);
    } catch (e) {
      alert(`Failed to restore version: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const canUnsubscribe = lead.email_status !== "unsubscribed" && lead.email_status !== "bounced";
  const nextFollowUp = pipelineConfig ? nextFollowUpDueAt(lead, pipelineConfig, schedule) : null;

  // What the "Current draft" panel is actually showing right now.
  const draftStageLabel = lead.draft_locked
    ? "Locked (sent)"
    : lead.last_sent_at
    ? `Upcoming: Follow-up ${lead.follow_up_count + 1}`
    : null;

  return (
    <>
      <p className="panel__eyebrow">Lead · {deriveStage(lead)}</p>
      <h2 className="panel__name">{lead.full_name}</h2>
      <p className="panel__meta">
        {[lead.position, lead.company].filter(Boolean).join(" · ") || "No company on file"}
      </p>
      <p className="panel__meta">
        {lead.email}
        {lead.linkedin_url && (
          <>
            {" · "}
            <a href={lead.linkedin_url} target="_blank" rel="noreferrer">
              Profile
            </a>
          </>
        )}
      </p>

      <dl className="info-list">
        <dt>Company</dt>
        <dd>{lead.company ?? "—"}</dd>
        <dt>Position</dt>
        <dd>{lead.position ?? "—"}</dd>
        <dt>Company size</dt>
        <dd>{lead.employee_count ? `${lead.employee_count} employees` : "—"}</dd>
        <dt>Location</dt>
        <dd>{lead.location ?? "—"}</dd>
        <dt>Follow-ups sent</dt>
        <dd>{lead.follow_up_count} / {pipelineConfig?.max_follow_ups ?? 3}</dd>
        <dt>Email status</dt>
        <dd style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {emailStatusLabel(lead.email_status)}
          {canUnsubscribe && (
            <button onClick={handleUnsubscribe} disabled={saving} style={{ padding: "3px 10px", fontSize: 12 }}>
              Unsubscribe
            </button>
          )}
        </dd>
        {lead.last_sent_at && (
          <>
            <dt>Last email sent</dt>
            <dd>{formatDateTime(lead.last_sent_at)}</dd>
          </>
        )}
        {nextFollowUp && (
          <>
            <dt>Next follow-up due</dt>
            <dd>{formatDateTime(nextFollowUp.toISOString())} — already drafted below</dd>
          </>
        )}
        {lead.website_clicked_at && (
          <>
            <dt>Website clicked</dt>
            <dd className="call-time">{formatDateTime(lead.website_clicked_at)}</dd>
          </>
        )}
        {lead.get_started_clicked_at && (
          <>
            <dt>Get started clicked</dt>
            <dd className="call-time">{formatDateTime(lead.get_started_clicked_at)}</dd>
          </>
        )}
        <dt>AI classification</dt>
        <dd>{lead.ai_response_classification ?? "pending"}</dd>
        <dt>Reply</dt>
        <dd>
          {lead.last_reply_text ? (
            <div className="reply-box">
              <p className="reply-box__text">{lead.last_reply_text}</p>
              {lead.last_reply_at && <span className="reply-box__when">Received {formatDateTime(lead.last_reply_at)}</span>}
            </div>
          ) : (
            "No reply yet"
          )}
        </dd>
        <dt>Requested availability</dt>
        <dd>{lead.requested_availability ?? "—"}</dd>
        <dt>Qualification</dt>
        <dd>{qualifiedStatusLabel(lead.qualified_status)}</dd>
      </dl>

      <CompanyResearch lead={lead} />

      {lead.ai_response_classification === "interested" && (
        <div className="qualbox">
          <h3>Qualification · {lead.qualified_status_source ?? "not yet reviewed"}</h3>
          <p className="reasoning">{lead.qualified_status_reasoning ?? "No AI reasoning captured."}</p>
          <div className="button-row">
            <button onClick={() => handleQualifyOverride("qualified")} disabled={saving}>
              Confirm qualified
            </button>
            <button onClick={() => handleQualifyOverride("not_qualified")} disabled={saving}>
              Not qualified
            </button>
          </div>
          <label>
            Confirm call time
            <input type="datetime-local" value={callTime} onChange={(e) => setCallTime(e.target.value)} />
          </label>
          <button onClick={handleConfirmCall} disabled={!callTime || saving} className="primary">
            Confirm call &amp; notify lead
          </button>
        </div>
      )}

      <div className="qualbox">
        <h3>Staff fields</h3>
        <label>
          Notes
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} />
        </label>
        <label>
          CRM status
          <input type="text" value={crmStatus} onChange={(e) => setCrmStatus(e.target.value)} placeholder="e.g. Move to CRM" />
        </label>
        <label>
          Sale status
          <input type="text" value={saleStatus} onChange={(e) => setSaleStatus(e.target.value)} placeholder="e.g. Converted Sale" />
        </label>
        <div className="button-row" style={{ alignItems: "center" }}>
          <button onClick={handleSaveManualFields} disabled={saving}>
            Save staff fields
          </button>
          {fieldsSaved && <span className="saved-indicator">Saved</span>}
        </div>
      </div>

      {/* ---- Current draft: always the live one, never swapped out ---- */}
      <div className="qualbox draft-panel">
        <h3>
          Current draft
          {draftStageLabel && <span className="locked-badge">{draftStageLabel}</span>}
        </h3>
        {!lead.current_draft_subject && !lead.draft_locked ? (
          <button onClick={handleGenerateDraft} disabled={saving} className="primary">
            Generate draft
          </button>
        ) : (
          <>
            <label>
              Subject
              <input type="text" value={subject} onChange={(e) => setSubject(e.target.value)} disabled={lead.draft_locked} />
            </label>
            <label>
              Body — just the message; a blank line starts a new paragraph. The logo, site links, and footer are
              added automatically.
              <textarea
                value={plainBody}
                onChange={(e) => setPlainBody(e.target.value)}
                rows={8}
                disabled={lead.draft_locked}
              />
            </label>
            {lead.personalization_note && <p className="personalization-note">{lead.personalization_note}</p>}
            <p className="chatbot-hint" style={{ marginTop: -4 }}>
              Preview — exactly what the lead will receive
            </p>
            {previewLoading && <div className="draft-preview">Loading preview…</div>}
            {!previewLoading && preview && (
              <iframe
                title="Email preview"
                srcDoc={preview.html}
                style={{ width: "100%", height: 420, border: "1px solid var(--border)", borderRadius: 10, background: "#fff" }}
              />
            )}
            {!lead.draft_locked && (
              <div className="button-row" style={{ marginTop: 14 }}>
                <button onClick={handleSaveDraft} disabled={saving || !isDirty}>
                  Save edits
                </button>
                <button onClick={handleSend} disabled={saving} className="primary">
                  Approve &amp; send
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* ---- Previous versions: plain read-only list, newest first ---- */}
      {lead.draft_history.length > 0 && (
        <div className="qualbox">
          <h3>Previous versions</h3>
          <div className="version-list">
            {[...lead.draft_history]
              .map((v, i) => ({ v, i }))
              .reverse()
              .map(({ v, i }) => (
                <div key={i} className="version-item">
                  <button type="button" className="version-item__toggle" onClick={() => toggleVersion(i)}>
                    {versionLabel(v)}
                  </button>
                  {expandedVersion === i && (
                    <div className="version-item__body">
                      {versionPreviews[i] ? (
                        <iframe
                          title={`Version ${i + 1} preview`}
                          srcDoc={versionPreviews[i].html}
                          style={{ width: "100%", height: 320, border: "1px solid var(--border)", borderRadius: 10, background: "#fff" }}
                        />
                      ) : (
                        <div className="draft-preview">Loading preview…</div>
                      )}
                      <div className="button-row" style={{ marginTop: 10 }}>
                        <button onClick={() => handleRestoreVersion(i)} disabled={saving} className="primary">
                          Restore this version
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              ))}
          </div>
        </div>
      )}

      <UnifiedLeadChat lead={lead} onDraftUpdated={onUpdate} />
    </>
  );
}
