import type { EmailStatus, Lead, QualifiedStatus } from "./types";

export type LeadFilter = "all" | "needs_review" | "qualified" | "stopped";

export const FILTERS: { key: LeadFilter; label: string }[] = [
  { key: "all", label: "All leads" },
  { key: "needs_review", label: "Needs review" },
  { key: "qualified", label: "Qualified" },
  { key: "stopped", label: "Stopped" },
];

export function applyFilter(leads: Lead[], filter: LeadFilter): Lead[] {
  switch (filter) {
    case "needs_review":
      return leads.filter((l) => l.ai_response_classification === "interested" && l.qualified_status === "unset");
    case "qualified":
      return leads.filter((l) => l.qualified_status === "qualified");
    case "stopped":
      return leads.filter((l) => ["bounced", "unsubscribed", "exhausted"].includes(l.email_status));
    default:
      return leads;
  }
}

export function filterCounts(leads: Lead[]): Record<LeadFilter, number> {
  return {
    all: leads.length,
    needs_review: applyFilter(leads, "needs_review").length,
    qualified: applyFilter(leads, "qualified").length,
    stopped: applyFilter(leads, "stopped").length,
  };
}

// "unset" is the raw enum value (§4 of the spec) meaning "no AI proposal or
// staff decision yet" -- shown to staff as something readable instead.
const QUALIFIED_STATUS_LABELS: Record<QualifiedStatus, string> = {
  unset: "Not yet reviewed",
  qualified: "Qualified",
  not_qualified: "Not qualified",
};

export function qualifiedStatusLabel(status: QualifiedStatus): string {
  return QUALIFIED_STATUS_LABELS[status];
}

// "bounced" is the internal status set by Gmail's own bounce detection
// (see backend send_batch.py) -- staff-facing copy calls it what it means:
// the address didn't work.
const EMAIL_STATUS_LABELS: Record<EmailStatus, string> = {
  not_sent: "Not sent",
  sent: "Sent",
  bounced: "Invalid email",
  unsubscribed: "Unsubscribed",
  exhausted: "No response",
};

export function emailStatusLabel(status: EmailStatus): string {
  return EMAIL_STATUS_LABELS[status];
}

export function initials(fullName: string): string {
  const parts = fullName.trim().split(/\s+/);
  const first = parts[0]?.[0] ?? "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + last).toUpperCase();
}

// ---- Column filters (company / stage / status / reply) -------------------
// Layered on top of the sidebar view (all / needs review / ...), so staff can
// narrow "All leads" to one company, one stage, and so on -- and the Excel
// download takes exactly what survives all of them.

export type ReplyFilter = "all" | "interested" | "not_interested" | "unclear" | "none";

export interface ColumnFilters {
  company: string; // normalized company key, "" = every company
  stage: string; // a deriveStage() label, "" = every stage
  status: QualifiedStatus | ""; // the Status column (qualification)
  reply: ReplyFilter; // the AI's read of the lead's reply
}

export const NO_FILTERS: ColumnFilters = { company: "", stage: "", status: "", reply: "all" };
export const NO_COMPANY_KEY = "__none__";

export function companyKey(lead: Lead): string {
  const name = (lead.company ?? "").trim().toLowerCase();
  return name || NO_COMPANY_KEY;
}

export function replyBucket(lead: Lead): Exclude<ReplyFilter, "all"> {
  const ai = lead.ai_response_classification;
  if (ai === "interested" || ai === "not_interested" || ai === "unclear") return ai;
  return "none";
}

export function applyColumnFilters(leads: Lead[], f: ColumnFilters, stageOf: (l: Lead) => string): Lead[] {
  return leads.filter(
    (l) =>
      (!f.company || companyKey(l) === f.company) &&
      (!f.stage || stageOf(l) === f.stage) &&
      (!f.status || l.qualified_status === f.status) &&
      (f.reply === "all" || replyBucket(l) === f.reply)
  );
}

export function activeFilterCount(f: ColumnFilters): number {
  return [f.company, f.stage, f.status, f.reply !== "all" ? f.reply : ""].filter(Boolean).length;
}

export function companyOptions(leads: Lead[]): { key: string; name: string; count: number }[] {
  const byKey = new Map<string, { key: string; name: string; count: number }>();
  for (const l of leads) {
    const key = companyKey(l);
    const entry = byKey.get(key) ?? { key, name: key === NO_COMPANY_KEY ? "(No company)" : (l.company ?? "").trim(), count: 0 };
    entry.count += 1;
    byKey.set(key, entry);
  }
  return [...byKey.values()].sort((a, b) => a.name.localeCompare(b.name));
}

export const REPLY_LABELS: Record<Exclude<ReplyFilter, "all">, string> = {
  interested: "Interested",
  not_interested: "Not interested",
  unclear: "Unclear",
  none: "No reply yet",
};
