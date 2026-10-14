import type { CompanyProfile, DraftChangeState, DraftPreview, FlowStage, ImportPreviewResult, Lead, Schedule } from "./types";
import type { DraftContent } from "./draftChanges.ts";
import { authEnabled, getIdToken } from "./firebase";

const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

async function authHeaders(): Promise<Record<string, string>> {
  if (!authEnabled) return {};
  const token = await getIdToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = {
    "Content-Type": "application/json",
    ...(await authHeaders()),
    ...(options.headers ?? {}),
  };
  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  listLeads: () => request<Lead[]>("/leads"),
  getLead: (id: string) => request<Lead>(`/leads/${id}`),

  previewImportColumns: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const headers = await authHeaders();
    const res = await fetch(`${BASE_URL}/leads/import/preview`, { method: "POST", body: form, headers });
    if (!res.ok) throw new Error(await res.text());
    return res.json() as Promise<ImportPreviewResult>;
  },

  importLeads: async (file: File, columnMap?: Record<string, string>) => {
    const form = new FormData();
    form.append("file", file);
    if (columnMap) form.append("column_map", JSON.stringify(columnMap));
    const headers = await authHeaders();
    const res = await fetch(`${BASE_URL}/leads/import`, { method: "POST", body: form, headers });
    if (!res.ok) throw new Error(await res.text());
    return res.json() as Promise<{ imported: number; skipped: number; skipped_detail: unknown[] }>;
  },

  updateManualFields: (id: string, fields: { notes?: string; crm_status?: string; sale_status?: string }) =>
    request<Lead>(`/leads/${id}/manual-fields`, { method: "PATCH", body: JSON.stringify(fields) }),

  overrideQualifiedStatus: (id: string, qualified_status: string, reasoning?: string) =>
    request<Lead>(`/leads/${id}/qualified-status`, {
      method: "POST",
      body: JSON.stringify({ qualified_status, reasoning }),
    }),

  confirmCall: (id: string, scheduled_call_time: string) =>
    request<Lead>(`/leads/${id}/confirm-call`, {
      method: "POST",
      body: JSON.stringify({ scheduled_call_time }),
    }),

  unsubscribeLead: (id: string) => request<Lead>(`/leads/${id}/unsubscribe`, { method: "POST" }),

  generateDrafts: (lead_ids: string[]) =>
    request<Lead[]>("/drafts/generate", { method: "POST", body: JSON.stringify({ lead_ids }) }),

  editDraftManually: (id: string, subject: string, body_html: string) =>
    request<Lead>(`/drafts/${id}`, { method: "PATCH", body: JSON.stringify({ subject, body_html }) }),

  editDraftViaChatbot: (id: string, instruction: string) =>
    request<Lead>(`/drafts/${id}/chatbot`, { method: "POST", body: JSON.stringify({ instruction }) }),

  restoreDraftVersion: (id: string, version_index: number) =>
    request<Lead>(`/drafts/${id}/restore-version`, { method: "POST", body: JSON.stringify({ version_index }) }),

  // Omit subject/body_html to preview the current draft; pass both to
  // preview arbitrary content (e.g. a superseded draft_history entry) --
  // same rendering the backend uses for a real send, not a second
  // hand-rolled one here.
  previewDraft: (id: string, subject?: string, body_html?: string) =>
    request<{ subject: string; html: string }>(`/drafts/${id}/preview`, {
      method: "POST",
      body: JSON.stringify(subject !== undefined ? { subject, body_html } : {}),
    }),

  askAboutLead: (id: string, question: string, history: { role: "staff" | "assistant"; text: string }[]) =>
    request<{ answer: string }>(`/drafts/${id}/ask`, { method: "POST", body: JSON.stringify({ question, history }) }),

  sendBatch: (lead_ids: string[]) =>
    request<{ sent: string[]; deferred_to_next_batch: string[]; errors: unknown[]; daily_cap: number; already_sent_today: number }>(
      "/pipeline/send",
      { method: "POST", body: JSON.stringify({ lead_ids }) }
    ),

  tick: () => request<{ lead_id: string; action: string }[]>("/pipeline/tick", { method: "POST" }),

  getPipelineConfig: () =>
    request<{ follow_up_wait_days: number; max_follow_ups: number; daily_send_cap: number }>("/pipeline/config"),

  // Deliberately not part of getPipelineConfig's cached response -- this is
  // live state (changes as sends actually happen), not fixed settings, so
  // the dashboard polls/refetches it independently.
  getSendStats: () => request<{ sent_today: number; daily_cap: number }>("/pipeline/send-stats"),

  getCompanyProfile: (id: string) => request<CompanyProfile | null>(`/leads/${id}/company-profile`),
  refreshCompanyProfile: (id: string) =>
    request<CompanyProfile | null>(`/leads/${id}/company-profile/refresh`, { method: "POST" }),

  getDraftFlow: () => request<FlowStage[]>("/draft-changes/flow"),
  getDraftChanges: () => request<DraftChangeState>("/draft-changes"),
  // Replaces the whole state; the server rejects it (409) if someone else saved since `expectedVersion`.
  saveDraftChanges: (expectedVersion: number, content: DraftContent) =>
    request<DraftChangeState>("/draft-changes", {
      method: "PUT",
      body: JSON.stringify({ expected_version: expectedVersion, changes: content.changes, saved: content.saved }),
    }),
  previewDraftChange: (lead_id: string, proposed?: { text: string; stages: string[] }) =>
    request<DraftPreview>("/draft-changes/preview", { method: "POST", body: JSON.stringify({ lead_id, proposed }) }),
  regenerateDrafts: (companies: string[] | null) =>
    request<{ regenerated: number; errors: unknown[] }>("/draft-changes/regenerate", {
      method: "POST",
      body: JSON.stringify({ companies }),
    }),

  getSchedule: () => request<Schedule>("/pipeline/schedule"),
  saveSchedule: (schedule: Schedule) =>
    request<Schedule>("/pipeline/schedule", { method: "PUT", body: JSON.stringify(schedule) }),

  // Excel of exactly the given leads (whatever the dashboard is showing).
  // Fetched as a blob so the auth header goes along, then handed to the
  // browser as a normal file download.
  exportLeads: async (lead_ids: string[], label: string) => {
    const res = await fetch(`${BASE_URL}/leads/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify({ lead_ids, label }),
    });
    if (!res.ok) throw new Error(await res.text());
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `${label || "leads"}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  sendFollowUpNow: (id: string) =>
    request<Lead>(`/pipeline/leads/${id}/send-follow-up-now`, { method: "POST" }),
};
