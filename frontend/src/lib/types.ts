// Mirrors backend/app/models/lead.py (CLAUDE.md §4) field-for-field.

export type EmailStatus = "not_sent" | "sent" | "bounced" | "unsubscribed" | "exhausted";
export type AIResponseClassification = "pending" | "interested" | "not_interested" | "unclear";
export type QualifiedStatus = "unset" | "qualified" | "not_qualified";
export type QualifiedStatusSource = "ai" | "staff";
export type DraftVersionSource = "initial" | "manual" | "chatbot" | "pipeline";

export interface DraftVersion {
  subject: string;
  body_html: string;
  source: DraftVersionSource;
  stage: string;
  // Whether this version actually went out, or was drafted/pre-generated
  // and then superseded before ever being sent (e.g. a pre-drafted
  // follow-up that never went out because a reply arrived first).
  sent: boolean;
  created_at: string;
}

export interface Lead {
  id: string;
  created_time: string;

  first_name: string | null;
  last_name: string | null;
  full_name: string;
  email: string;
  linkedin_url: string | null;
  company: string | null;
  position: string | null;
  employee_count: string | null;
  location: string | null;

  thread_id: string | null;
  last_sent_at: string | null;
  follow_up_count: number;
  email_status: EmailStatus;

  ai_response_classification: AIResponseClassification | null;
  requested_availability: string | null;
  last_reply_text: string | null;
  last_reply_at: string | null;
  website_clicked_at: string | null;
  get_started_clicked_at: string | null;

  qualified_status: QualifiedStatus;
  qualified_status_source: QualifiedStatusSource | null;
  qualified_status_reasoning: string | null;

  crm_status: string | null;
  sale_status: string | null;
  notes: string | null;
  scheduled_call_time: string | null;

  current_draft_subject: string | null;
  current_draft_body_html: string | null;
  draft_locked: boolean;
  // Which of the 27 challenges the draft was framed around, and why.
  personalization_note: string | null;
  draft_history: DraftVersion[];
}

export interface ImportColumnField {
  field: string;
  label: string;
  required: boolean;
  matched_column: string | null;
}

export interface ImportPreviewResult {
  columns: string[];
  fields: ImportColumnField[];
}

// Follow-up wait (backend app/services/schedule_service.py): the default,
// plus overrides per company and per follow-up stage ("1" = the wait after
// the initial email, "2" = after follow-up 1, ...). Company beats stage.
export interface Schedule {
  default_days: number;
  by_company: Record<string, number>;
  by_stage: Record<string, number>;
}

export interface ChallengeMatch {
  id: number;
  name: string;
  evidence: string;
}

// What the company-research step learned about a lead's company (shared by
// every lead at that company). status "not_found" = couldn't be researched.
export interface CompanyProfile {
  key: string;
  company: string;
  status: "ok" | "not_found";
  website: string | null;
  found_via: "email_domain" | "search" | null;
  source_url: string | null;
  summary: string | null;
  stage_hint: "starting" | "growing" | "established" | "unknown";
  challenges: ChallengeMatch[];
  researched_at: string;
}

// ---- Modify draft ----------------------------------------------------------
export interface FlowStage {
  id: string;
  no: number;
  title: string;
  description: string;
  chips: string[];
  builtin_rule: string | null;
  editable: boolean;
}

export interface DraftChangeState {
  version: number;
  changes: import("./draftChanges.ts").DraftChange[];
  saved: import("./draftChanges.ts").SavedChange[];
}

export interface RenderedDraft {
  subject: string;
  html: string;
}

export interface DraftPreview {
  before: RenderedDraft;
  after: RenderedDraft;
  simulated: boolean;
}
