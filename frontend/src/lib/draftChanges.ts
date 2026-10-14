// Pure logic behind the Modify draft page: applying a change, naming it,
// reusing it, Undo and "Back to initial state". No React and no network in
// here -- the page computes the next content with these, saves it to the
// server, and only then records it in the session history. That keeps every
// rule testable on its own (draftChanges.test.ts).

export const ALL = "all"; // "entire email" as a stage id

export interface DraftChange {
  id: string;
  text: string;
  stages: string[]; // ["all"] or specific stage ids
  companies: string[] | null; // null = every company
  name: string | null;
  enabled: boolean;
  created_at: string;
}

export interface SavedChange {
  name: string;
  text: string;
  stages: string[];
}

export interface DraftContent {
  changes: DraftChange[];
  saved: SavedChange[];
}

export interface Proposal {
  text: string;
  stages: string[];
  companies: string[] | null;
  name: string;
  reuseOf: string | null; // the saved change this proposal was started from, if any
}

export interface HistoryEntry {
  label: string;
  before: DraftContent;
}

export interface Session {
  content: DraftContent;
  history: HistoryEntry[];
}

export const EMPTY_CONTENT: DraftContent = { changes: [], saved: [] };
export const MAX_HISTORY = 50;
export const MAX_TEXT = 500;

export function newSession(content: DraftContent = EMPTY_CONTENT): Session {
  return { content, history: [] };
}

export const norm = (s: string | null | undefined): string => (s ?? "").trim().replace(/\s+/g, " ").toLowerCase();

// "Entire email" covers every stage, so ticking it replaces any specific ones.
export function normalizeStages(stages: string[]): string[] {
  const unique = [...new Set(stages)];
  return unique.includes(ALL) ? [ALL] : unique;
}

// Wording that asks for something the prompt's hard rules forbid. A warning,
// not a block: the model ignores that part, and the page says so up front.
const FORBIDDEN = /pric\w*|discount|guarantee|\d+\s?%|\broi\b|case study|save .*\$|\$\d/i;
export function warningFor(text: string): string | null {
  return FORBIDDEN.test(text)
    ? "That asks for a price, a statistic or a guaranteed result. The prompt's hard rules forbid inventing those, so the model will ignore that part. Reword it, or add the fact to vrt_company.md first."
    : null;
}

export function emptyProposal(): Proposal {
  return { text: "", stages: [ALL], companies: null, name: "", reuseOf: null };
}

export function isInitial(content: DraftContent): boolean {
  return content.changes.length === 0 && content.saved.length === 0;
}

export function canApply(p: Proposal): boolean {
  const text = p.text.trim();
  return text.length > 0 && text.length <= MAX_TEXT && p.stages.length > 0 && (p.companies === null || p.companies.length > 0);
}

/* ---- applying and editing ------------------------------------------------ */

export function applyProposal(
  content: DraftContent,
  proposal: Proposal,
  makeId: () => string,
  now: string
): { content: DraftContent; label: string } {
  const text = proposal.text.trim().replace(/\s+/g, " ");
  const name = proposal.name.trim().replace(/\s+/g, " ") || null;
  const stages = normalizeStages(proposal.stages);

  const change: DraftChange = {
    id: makeId(),
    text,
    stages,
    companies: proposal.companies === null ? null : [...new Set(proposal.companies)],
    name,
    enabled: true,
    created_at: now,
  };

  // A named change is also kept in the library, replacing any earlier one of the same name.
  let saved = content.saved;
  if (name) {
    const entry: SavedChange = { name, text, stages };
    const at = saved.findIndex((s) => norm(s.name) === norm(name));
    saved = at === -1 ? [...saved, entry] : saved.map((s, i) => (i === at ? entry : s));
  }

  return {
    content: { changes: [...content.changes, change], saved },
    label: name ? `Applied "${name}"` : "Applied a change",
  };
}

export function toggleChange(content: DraftContent, id: string): { content: DraftContent; label: string } {
  const target = content.changes.find((c) => c.id === id);
  return {
    content: { ...content, changes: content.changes.map((c) => (c.id === id ? { ...c, enabled: !c.enabled } : c)) },
    label: `${target?.enabled ? "Turned off" : "Turned on"} an instruction`,
  };
}

export function removeChange(content: DraftContent, id: string): { content: DraftContent; label: string } {
  return { content: { ...content, changes: content.changes.filter((c) => c.id !== id) }, label: "Removed an instruction" };
}

// Deleting a saved change removes it from the library only; instructions
// already applied from it stay (they are changes in their own right).
export function deleteSaved(content: DraftContent, name: string): { content: DraftContent; label: string } {
  return {
    content: { ...content, saved: content.saved.filter((s) => norm(s.name) !== norm(name)) },
    label: `Deleted saved change "${name}"`,
  };
}

/* ---- reusing a saved change ---------------------------------------------- */

// Companies (by display name) an enabled change of this name already covers.
export function coveredBy(content: DraftContent, name: string, allCompanies: string[]): Set<string> {
  const covered = new Set<string>();
  for (const c of content.changes) {
    if (!c.enabled || norm(c.name) !== norm(name)) continue;
    for (const company of allCompanies) {
      if (c.companies === null || c.companies.some((x) => norm(x) === norm(company))) covered.add(company);
    }
  }
  return covered;
}

// Starts a new proposal from a saved change, pre-selecting the companies it
// isn't on yet (or all companies, when it already is everywhere).
export function proposalFromSaved(content: DraftContent, saved: SavedChange, allCompanies: string[]): Proposal {
  const covered = coveredBy(content, saved.name, allCompanies);
  const uncovered = allCompanies.filter((c) => !covered.has(c));
  return {
    text: saved.text,
    stages: [...saved.stages],
    companies: uncovered.length > 0 && uncovered.length < allCompanies.length ? uncovered : null,
    name: saved.name,
    reuseOf: saved.name,
  };
}

/* ---- Undo and Back to initial state ---------------------------------------- */

// Records `next` as the new content, remembering what it replaced so Undo can restore it.
export function commit(session: Session, label: string, next: DraftContent): Session {
  const history = [...session.history, { label, before: session.content }].slice(-MAX_HISTORY);
  return { content: next, history };
}

export function lastStep(session: Session): HistoryEntry | null {
  return session.history.at(-1) ?? null;
}

// The session after undoing the most recent step, or null when there is nothing to undo.
export function undo(session: Session): Session | null {
  const step = lastStep(session);
  return step ? { content: step.before, history: session.history.slice(0, -1) } : null;
}

// "Back to initial state" is itself a step, so it can be undone.
export function resetAll(session: Session): Session {
  return commit(session, "Back to initial state", EMPTY_CONTENT);
}

/* ---- what the flowchart and the lists show ----------------------------------- */

export function changesForStage(changes: DraftChange[], stageId: string, editable: boolean): DraftChange[] {
  if (!editable) return []; // the sign-off and links stage never carries instructions
  return changes.filter((c) => c.enabled && (c.stages.includes(ALL) || c.stages.includes(stageId)));
}

export function coversCompany(change: { companies: string[] | null }, company: string | null): boolean {
  return change.companies === null || change.companies.some((c) => norm(c) === norm(company));
}

export interface UnsentLead {
  company: string | null;
  email_status: string;
  draft_locked: boolean;
}

// Drafts "Also rewrite" would touch: never sent, not locked, company in scope.
export function unsentInScope(leads: UnsentLead[], companies: string[] | null): number {
  return leads.filter(
    (l) => l.email_status === "not_sent" && !l.draft_locked && (companies === null || coversCompany({ companies }, l.company))
  ).length;
}

export function describeScope(change: { companies: string[] | null }): string {
  if (change.companies === null) return "All companies";
  return change.companies.length <= 3 ? change.companies.join(", ") : `${change.companies.length} companies`;
}
