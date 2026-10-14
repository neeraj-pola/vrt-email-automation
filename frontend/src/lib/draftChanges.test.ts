// Run: npm test   (Node's built-in test runner; it strips the TypeScript types itself)
import test from "node:test";
import assert from "node:assert/strict";
import {
  ALL,
  EMPTY_CONTENT,
  MAX_HISTORY,
  applyProposal,
  canApply,
  changesForStage,
  commit,
  coveredBy,
  deleteSaved,
  describeScope,
  emptyProposal,
  isInitial,
  lastStep,
  newSession,
  normalizeStages,
  proposalFromSaved,
  removeChange,
  resetAll,
  toggleChange,
  undo,
  unsentInScope,
  warningFor,
  type DraftContent,
  type Proposal,
  type Session,
} from "./draftChanges.ts";

const COMPANIES = ["Acme Industrial", "Bluepeak Logistics", "Kiran Digital", "Northwind Advisory", "Reilly Freight"];
const EDITABLE = ["lead", "research", "angle", "intro", "offer", "cta"];

let n = 0;
const makeId = () => `id-${++n}`;
const NOW = "2026-10-08T10:00:00Z";

const proposal = (over: Partial<Proposal> = {}): Proposal => ({ ...emptyProposal(), text: "Keep it short", ...over });

// A tiny stand-in for the page: apply -> commit to the session, exactly as the page does after a save.
function applyTo(session: Session, over: Partial<Proposal> = {}): Session {
  const r = applyProposal(session.content, proposal(over), makeId, NOW);
  return commit(session, r.label, r.content);
}

/* ============ 1. each part of the email, one at a time ============ */

for (const stage of EDITABLE) {
  test(`a change aimed at "${stage}" shows on that stage only`, () => {
    const s = applyTo(newSession(), { stages: [stage], text: `marker for ${stage}` });
    for (const other of [...EDITABLE, "links"]) {
      const shown = changesForStage(s.content.changes, other, other !== "links").map((c) => c.text);
      assert.equal(shown.includes(`marker for ${stage}`), other === stage, `aimed at ${stage}, looking at ${other}`);
    }
  });
}

test("the locked sign-off and links stage never carries an instruction, even from 'entire email'", () => {
  const s = applyTo(newSession(), { stages: [ALL] });
  assert.deepEqual(changesForStage(s.content.changes, "links", false), []);
});

/* ============ 2. the entire email ============ */

test("'entire email' is stored once and shows on every editable stage", () => {
  const s = applyTo(newSession(), { stages: [ALL], text: "plain words" });
  assert.equal(s.content.changes.length, 1);
  assert.deepEqual(s.content.changes[0].stages, [ALL]);
  for (const st of EDITABLE) assert.equal(changesForStage(s.content.changes, st, true).length, 1, st);
});

test("ticking 'entire email' replaces any specific stages", () => {
  assert.deepEqual(normalizeStages(["intro", ALL, "cta"]), [ALL]);
  assert.deepEqual(normalizeStages(["intro", "intro", "cta"]), ["intro", "cta"]);
});

test("one change over several chosen stages is one change that shows on each of them", () => {
  const s = applyTo(newSession(), { stages: ["intro", "offer", "cta"] });
  assert.equal(s.content.changes.length, 1);
  for (const st of ["intro", "offer", "cta"]) assert.equal(changesForStage(s.content.changes, st, true).length, 1, st);
  assert.equal(changesForStage(s.content.changes, "angle", true).length, 0);
});

test("a change can't be applied with no text, too much text, no stage, or an empty company list", () => {
  assert.equal(canApply(proposal()), true);
  assert.equal(canApply(proposal({ text: "   " })), false);
  assert.equal(canApply(proposal({ text: "x".repeat(501) })), false);
  assert.equal(canApply(proposal({ stages: [] })), false);
  assert.equal(canApply(proposal({ companies: [] })), false);
  assert.equal(canApply(proposal({ companies: null })), true);
});

test("applied text is tidied (trimmed, single spaces)", () => {
  const s = applyTo(newSession(), { text: "  keep   it \n short " });
  assert.equal(s.content.changes[0].text, "keep it short");
});

/* ============ 3. company scope ============ */

test("a change scoped to chosen companies only covers those", () => {
  const s = applyTo(newSession(), { companies: ["Acme Industrial", "Kiran Digital"] });
  assert.deepEqual(s.content.changes[0].companies, ["Acme Industrial", "Kiran Digital"]);
  assert.equal(describeScope(s.content.changes[0]), "Acme Industrial, Kiran Digital");
});

test("null companies means every company", () => {
  const s = applyTo(newSession(), { companies: null });
  assert.equal(s.content.changes[0].companies, null);
  assert.equal(describeScope(s.content.changes[0]), "All companies");
});

test("a long company list is summarized by count", () => {
  assert.equal(describeScope({ companies: COMPANIES }), "5 companies");
});

test("'also rewrite' counts only unsent, unlocked drafts in scope (company match ignores case and spacing)", () => {
  const leads = [
    { company: "Acme Industrial", email_status: "not_sent", draft_locked: false },
    { company: "  ACME  industrial", email_status: "not_sent", draft_locked: false },
    { company: "Acme Industrial", email_status: "sent", draft_locked: true },
    { company: "Acme Industrial", email_status: "not_sent", draft_locked: true },
    { company: "Reilly Freight", email_status: "not_sent", draft_locked: false },
    { company: null, email_status: "not_sent", draft_locked: false },
  ];
  assert.equal(unsentInScope(leads, ["Acme Industrial"]), 2);
  assert.equal(unsentInScope(leads, null), 4);
  assert.equal(unsentInScope(leads, ["Reilly Freight", "Acme Industrial"]), 3);
});

/* ============ 4. undo ============ */

test("there is nothing to undo at the start", () => {
  assert.equal(undo(newSession()), null);
  assert.equal(lastStep(newSession()), null);
});

test("undo removes the last applied change and its saved name", () => {
  let s = applyTo(newSession(), { stages: ["intro"], name: "Casual intro" });
  assert.equal(s.content.changes.length, 1);
  assert.equal(s.content.saved.length, 1);
  s = undo(s)!;
  assert.deepEqual(s.content, EMPTY_CONTENT);
  assert.equal(undo(s), null);
});

test("undo steps back one change at a time, newest first", () => {
  let s = newSession();
  for (const st of ["intro", "offer", "cta"]) s = applyTo(s, { stages: [st], text: `for ${st}` });
  assert.deepEqual(s.content.changes.map((c) => c.stages[0]), ["intro", "offer", "cta"]);
  s = undo(s)!;
  assert.deepEqual(s.content.changes.map((c) => c.stages[0]), ["intro", "offer"]);
  s = undo(s)!;
  assert.deepEqual(s.content.changes.map((c) => c.stages[0]), ["intro"]);
  s = undo(s)!;
  assert.equal(s.content.changes.length, 0);
});

test("the undo step names what it will undo", () => {
  let s = applyTo(newSession(), { name: "Casual intro" });
  assert.equal(lastStep(s)?.label, 'Applied "Casual intro"');
  s = applyTo(s);
  assert.equal(lastStep(s)?.label, "Applied a change");
});

test("undo reverses turning off, removing, and deleting a saved change", () => {
  let s = applyTo(newSession(), { name: "Casual intro" });
  const id = s.content.changes[0].id;

  let r = toggleChange(s.content, id);
  s = commit(s, r.label, r.content);
  assert.equal(s.content.changes[0].enabled, false);
  s = undo(s)!;
  assert.equal(s.content.changes[0].enabled, true);

  r = removeChange(s.content, id);
  s = commit(s, r.label, r.content);
  assert.equal(s.content.changes.length, 0);
  s = undo(s)!;
  assert.equal(s.content.changes.length, 1);

  r = deleteSaved(s.content, "Casual intro");
  s = commit(s, r.label, r.content);
  assert.equal(s.content.saved.length, 0);
  s = undo(s)!;
  assert.equal(s.content.saved.length, 1);
});

test("a turned-off change stops showing on its stage", () => {
  let s = applyTo(newSession(), { stages: ["cta"] });
  assert.equal(changesForStage(s.content.changes, "cta", true).length, 1);
  const r = toggleChange(s.content, s.content.changes[0].id);
  s = commit(s, r.label, r.content);
  assert.equal(changesForStage(s.content.changes, "cta", true).length, 0);
});

test("undo never mutates the content it restores (snapshots are independent)", () => {
  let s = applyTo(newSession(), { text: "one" });
  const snapshot = JSON.stringify(s.content);
  s = applyTo(s, { text: "two" });
  s = undo(s)!;
  assert.equal(JSON.stringify(s.content), snapshot);
  s = applyTo(s, { text: "three" });
  assert.deepEqual(s.content.changes.map((c) => c.text), ["one", "three"]);
});

test("history keeps the 50 most recent steps", () => {
  let s = newSession();
  for (let i = 0; i < 60; i++) s = applyTo(s, { text: `change ${i}` });
  assert.equal(s.history.length, MAX_HISTORY);
  while (undo(s)) s = undo(s)!;
  assert.equal(s.content.changes.length, 10); // the 10 oldest are beyond reach
});

/* ============ 5. back to initial state ============ */

test("a fresh session is the initial state", () => {
  assert.equal(isInitial(newSession().content), true);
});

test("a change, or a saved change alone, means it isn't initial any more", () => {
  assert.equal(isInitial(applyTo(newSession()).content), false);
  assert.equal(isInitial({ changes: [], saved: [{ name: "A", text: "x", stages: [ALL] }] }), false);
});

test("back to initial state clears every change and saved change", () => {
  let s = newSession();
  s = applyTo(s, { name: "A", stages: ["intro"] });
  s = applyTo(s, { name: "B", stages: ["cta"], companies: ["Acme Industrial"] });
  s = applyTo(s);
  s = resetAll(s);
  assert.deepEqual(s.content, EMPTY_CONTENT);
  assert.equal(isInitial(s.content), true);
});

test("back to initial state can itself be undone, restoring changes and saved changes", () => {
  let s = newSession();
  s = applyTo(s, { name: "A", stages: ["intro"] });
  s = applyTo(s, { name: "B", stages: ["cta"] });
  s = resetAll(s);
  assert.equal(lastStep(s)?.label, "Back to initial state");
  s = undo(s)!;
  assert.equal(s.content.changes.length, 2);
  assert.deepEqual(s.content.saved.map((x) => x.name), ["A", "B"]);
});

test("after a reset the session keeps working normally", () => {
  let s = applyTo(newSession(), { text: "a" });
  s = resetAll(s);
  s = applyTo(s, { text: "b" });
  assert.deepEqual(s.content.changes.map((c) => c.text), ["b"]);
  const ids = s.content.changes.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length);
});

/* ============ 6. saving a change with a name ============ */

test("a named change is saved with its name, wording and stages", () => {
  const s = applyTo(newSession(), { name: "Assessment offer", text: "Mention the free 7 Stages assessment", stages: ["offer"] });
  assert.deepEqual(s.content.saved, [{ name: "Assessment offer", text: "Mention the free 7 Stages assessment", stages: ["offer"] }]);
  assert.equal(s.content.changes[0].name, "Assessment offer");
});

test("an unnamed change is applied but not saved for reuse", () => {
  const s = applyTo(newSession(), { name: "" });
  assert.equal(s.content.saved.length, 0);
  assert.equal(s.content.changes[0].name, null);
});

test("a whitespace-only name counts as unnamed", () => {
  const s = applyTo(newSession(), { name: "   " });
  assert.equal(s.content.saved.length, 0);
  assert.equal(s.content.changes[0].name, null);
});

test("saving a second change under the same name replaces the saved copy (case-insensitive)", () => {
  let s = applyTo(newSession(), { name: "My tweak", text: "first", stages: ["intro"] });
  s = applyTo(s, { name: "my TWEAK", text: "second", stages: ["cta"] });
  assert.equal(s.content.saved.length, 1);
  assert.equal(s.content.saved[0].text, "second");
  assert.deepEqual(s.content.saved[0].stages, ["cta"]);
  assert.equal(s.content.changes.length, 2); // both applied changes are kept
});

test("a change over several stages is saved as one named change covering all of them", () => {
  const s = applyTo(newSession(), { name: "Casual both", stages: ["intro", "cta"] });
  assert.equal(s.content.saved.length, 1);
  assert.deepEqual(s.content.saved[0].stages, ["intro", "cta"]);
});

test("reuse pre-fills the saved change and pre-selects the companies it isn't on yet", () => {
  const s = applyTo(newSession(), { name: "Casual intro", stages: ["intro"], companies: ["Acme Industrial"] });
  const p = proposalFromSaved(s.content, s.content.saved[0], COMPANIES);
  assert.equal(p.text, "Keep it short");
  assert.equal(p.name, "Casual intro");
  assert.equal(p.reuseOf, "Casual intro");
  assert.deepEqual(p.stages, ["intro"]);
  assert.deepEqual(p.companies, COMPANIES.filter((c) => c !== "Acme Industrial"));
});

test("reusing a change that already covers every company pre-selects 'all companies'", () => {
  const s = applyTo(newSession(), { name: "Everywhere", companies: null });
  assert.equal(proposalFromSaved(s.content, s.content.saved[0], COMPANIES).companies, null);
});

test("reusing a change that covers none of the companies pre-selects 'all companies' too", () => {
  const content: DraftContent = { changes: [], saved: [{ name: "Fresh", text: "x", stages: ["cta"] }] };
  assert.equal(proposalFromSaved(content, content.saved[0], COMPANIES).companies, null);
});

test("reuse on another company adds a change and keeps a single saved copy", () => {
  let s = applyTo(newSession(), { name: "Casual intro", stages: ["intro"], companies: ["Acme Industrial"] });
  const p = proposalFromSaved(s.content, s.content.saved[0], COMPANIES);
  s = applyTo(s, { ...p, companies: ["Kiran Digital"] });
  assert.equal(s.content.saved.length, 1);
  assert.equal(s.content.changes.length, 2);
  assert.deepEqual([...coveredBy(s.content, "Casual intro", COMPANIES)].sort(), ["Acme Industrial", "Kiran Digital"]);
});

test("coverage ignores turned-off changes", () => {
  let s = applyTo(newSession(), { name: "N", companies: ["Acme Industrial"] });
  const r = toggleChange(s.content, s.content.changes[0].id);
  s = commit(s, r.label, r.content);
  assert.equal(coveredBy(s.content, "N", COMPANIES).size, 0);
});

test("deleting a saved change removes the library entry but keeps the applied instructions", () => {
  let s = applyTo(newSession(), { name: "Casual intro" });
  const r = deleteSaved(s.content, "casual INTRO");
  s = commit(s, r.label, r.content);
  assert.equal(s.content.saved.length, 0);
  assert.equal(s.content.changes.length, 1);
});

test("ids stay unique across undo and re-apply", () => {
  let s = applyTo(newSession());
  s = undo(s)!;
  s = applyTo(s);
  s = applyTo(s);
  const ids = s.content.changes.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length);
});

/* ============ 7. wording that asks for something the rules forbid ============ */

test("requests for prices, statistics or guarantees are flagged", () => {
  for (const text of ["Promise 30% growth in a year", "mention our pricing", "offer a discount", "guarantee results", "include the ROI", "add a case study", "say clients save $50k"]) {
    assert.notEqual(warningFor(text), null, text);
  }
});

test("ordinary wording is not flagged, including words that merely contain 'roi'", () => {
  for (const text of ["support Android users", "mention the free assessment", "sound more casual", "avoid heroic claims", "end with a question"]) {
    assert.equal(warningFor(text), null, text);
  }
});
