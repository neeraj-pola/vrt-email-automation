import type { Lead, Schedule } from "./types";
import type { PipelineConfig } from "./config";

// Derives a single human-readable "stage" label from the underlying status
// fields, for the dense list view (CLAUDE.md §9: "which follow-up stage it's
// at or which stage it responded at").
export function deriveStage(lead: Lead): string {
  if (lead.email_status === "bounced") return "Invalid email";
  if (lead.email_status === "unsubscribed") return "Unsubscribed";
  if (lead.email_status === "exhausted") return "No response (exhausted)";

  if (lead.ai_response_classification === "interested") {
    if (lead.scheduled_call_time) return "Call scheduled";
    if (lead.qualified_status === "qualified") return "Qualified — awaiting call";
    if (lead.qualified_status === "not_qualified") return "Interested — not qualified";
    return "Interested — awaiting review";
  }
  if (lead.ai_response_classification === "not_interested") return "Not interested";
  if (lead.ai_response_classification === "unclear") return "Unclear reply";

  if (lead.email_status === "not_sent") {
    return lead.current_draft_subject ? "Draft ready" : "Not contacted";
  }
  if (lead.email_status === "sent") {
    return lead.follow_up_count > 0 ? `Follow-up ${lead.follow_up_count} sent` : "Initial email sent";
  }
  return "Unknown";
}

// The date the *initial* outreach email actually went out -- distinct from
// created_time (when the lead filled out the ad form, often long before
// this system ever contacted them) and from last_sent_at (which moves
// forward on every follow-up, not just the first send). Read from
// draft_history's "Initial outreach" entry rather than last_sent_at, so
// this stays correct even after follow-ups have gone out since.
export function initialSendDate(lead: Lead): string | null {
  const initial = lead.draft_history.find((v) => v.stage === "Initial outreach" && v.sent);
  if (initial) return initial.created_at;
  // Never had a follow-up yet, so last_sent_at (if any) can only be from
  // the initial send -- covers a lead sent before draft_history existed.
  if (lead.follow_up_count === 0 && lead.last_sent_at) return lead.last_sent_at;
  return null;
}

export function stageTone(lead: Lead): "neutral" | "positive" | "negative" | "warning" {
  if (lead.email_status === "bounced" || lead.email_status === "unsubscribed") return "negative";
  if (lead.ai_response_classification === "not_interested") return "negative";
  if (lead.ai_response_classification === "interested") return "positive";
  if (lead.email_status === "exhausted" || lead.ai_response_classification === "unclear") return "warning";
  return "neutral";
}

// True for a lead sitting in staff's court right now -- an AI-proposed
// qualification nobody has confirmed/overridden yet, or a reply too vague
// to auto-route. Distinct from stageTone: a stage can be "positive"-toned
// (e.g. any interested reply) without actually needing anyone to act today.
export function needsAttention(lead: Lead): boolean {
  if (lead.ai_response_classification === "interested" && lead.qualified_status === "unset") return true;
  if (lead.ai_response_classification === "unclear") return true;
  return false;
}

export function hasScheduledCall(lead: Lead): boolean {
  return Boolean(lead.scheduled_call_time);
}

// Mirrors the backend's next_follow_up_due_at (app/pipeline/outreach_graph.py)
// exactly, so the dashboard can show/recompute this without an extra
// round-trip per lead. Always derived fresh from last_sent_at -- sending a
// follow-up early moves last_sent_at to when it actually went out, which
// automatically pushes this forward from there, not from the original
// +wait_days schedule.
// Same precedence as the backend (schedule_service.Schedule.wait_days_for):
// a company override, then the override for this follow-up stage, then the
// default.
export function waitDaysFor(lead: Lead, config: PipelineConfig, schedule?: Schedule | null): number {
  if (!schedule) return config.follow_up_wait_days;
  const company = (lead.company ?? "").trim().toLowerCase();
  if (company) {
    for (const [name, days] of Object.entries(schedule.by_company)) {
      if (name.trim().toLowerCase() === company) return days;
    }
  }
  const stage = String(lead.follow_up_count + 1);
  return schedule.by_stage[stage] ?? schedule.default_days;
}

export function nextFollowUpDueAt(lead: Lead, config: PipelineConfig, schedule?: Schedule | null): Date | null {
  if (lead.email_status !== "sent" || !lead.last_sent_at) return null;
  if (lead.ai_response_classification === "interested" || lead.ai_response_classification === "not_interested") {
    return null; // terminal -- automatic follow-up loop stops (§6.7)
  }
  if (lead.follow_up_count >= config.max_follow_ups) return null;
  const due = new Date(lead.last_sent_at);
  due.setDate(due.getDate() + waitDaysFor(lead, config, schedule));
  return due;
}

export function canSendFollowUpNow(lead: Lead, config: PipelineConfig, schedule?: Schedule | null): boolean {
  return nextFollowUpDueAt(lead, config, schedule) !== null;
}

// Ordered coarse-to-fine so a "sort by stage" view surfaces what needs
// action first, then in-progress outreach, then closed-out leads last.
const STAGE_RANK: Record<string, number> = {
  "Interested — awaiting review": 0,
  "Unclear reply": 1,
  "Call scheduled": 2,
  "Qualified — awaiting call": 3,
  "Draft ready": 4,
  "Initial email sent": 5,
  "Not contacted": 6,
  "Interested — not qualified": 7,
  "Not interested": 8,
  "No response (exhausted)": 9,
  "Invalid email": 10,
  Unsubscribed: 11,
};

export function stageRank(lead: Lead): number {
  const stage = deriveStage(lead);
  if (stage in STAGE_RANK) return STAGE_RANK[stage];
  if (stage.startsWith("Follow-up")) return 5; // group with "in progress"
  return 99;
}
