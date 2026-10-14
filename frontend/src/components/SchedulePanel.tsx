import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { getSchedule, setCachedSchedule } from "../lib/config";
import { companyOptions } from "../lib/filters";
import type { Lead, Schedule } from "../lib/types";

interface Props {
  leads: Lead[]; // to offer the companies that actually exist
  maxFollowUps: number;
}

const STAGE_LABEL = (n: number) => (n === 1 ? "After the first email" : `After follow-up ${n - 1}`);

function describe(s: Schedule): string {
  const extras = Object.keys(s.by_company).length + Object.keys(s.by_stage).length;
  return extras === 0 ? "" : ` · ${extras} custom rule${extras === 1 ? "" : "s"}`;
}

// The follow-up wait, always visible at the top of the leads page. Default
// is 3 days; staff can override it for a company and/or for a specific
// follow-up stage. The most specific rule wins: company, then stage, then
// the default (same order as the backend's schedule_service).
export default function SchedulePanel({ leads, maxFollowUps }: Props) {
  const [saved, setSaved] = useState<Schedule | null>(null);
  const [draft, setDraft] = useState<Schedule | null>(null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newCompany, setNewCompany] = useState("");
  const [newCompanyDays, setNewCompanyDays] = useState("");

  useEffect(() => {
    getSchedule()
      .then((s) => {
        setSaved(s);
        setDraft(s);
      })
      .catch((e) => setError(String(e)));
  }, []);

  if (!saved || !draft) {
    return error ? <div className="scheduler scheduler--error">Couldn't load the follow-up schedule: {error}</div> : null;
  }

  const companies = companyOptions(leads).filter((c) => c.key !== "__none__");
  const dirty = JSON.stringify(saved) !== JSON.stringify(draft);
  const stages = Array.from({ length: maxFollowUps }, (_, i) => i + 1);

  const parseDays = (raw: string): number | null => {
    const n = Number(raw);
    return Number.isInteger(n) && n >= 1 && n <= 90 ? n : null;
  };

  const setStage = (stage: number, raw: string) => {
    const by_stage = { ...draft.by_stage };
    const n = parseDays(raw);
    if (raw === "" || n === null) delete by_stage[String(stage)];
    else by_stage[String(stage)] = n;
    setDraft({ ...draft, by_stage });
  };

  const setCompany = (name: string, days: number | null) => {
    const by_company = { ...draft.by_company };
    if (days === null) delete by_company[name];
    else by_company[name] = days;
    setDraft({ ...draft, by_company });
  };

  const addCompany = () => {
    const days = parseDays(newCompanyDays);
    if (!newCompany || days === null) return;
    setCompany(newCompany, days);
    setNewCompany("");
    setNewCompanyDays("");
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const result = await api.saveSchedule(draft);
      setCachedSchedule(result);
      setSaved(result);
      setDraft(result);
      setOpen(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="scheduler" aria-label="Follow-up schedule">
      <div className="scheduler__summary">
        <span className="scheduler__title">Follow-up schedule</span>
        <span className="scheduler__value">
          Wait <strong>{saved.default_days}</strong> day{saved.default_days === 1 ? "" : "s"} between emails
          {describe(saved)}
        </span>
        <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? "Close" : "Edit schedule"}
        </button>
      </div>

      {open && (
        <div className="scheduler__editor">
          <p className="scheduler__help">
            Days to wait for a reply before the next follow-up goes out. A company rule beats a stage rule, and a stage
            rule beats the default. Changes apply to leads already waiting, from their last email.
          </p>

          <div className="scheduler__group">
            <h3>Default</h3>
            <label className="scheduler__row">
              All leads
              <input
                type="number"
                min={1}
                max={90}
                value={draft.default_days}
                onChange={(e) => {
                  const n = parseDays(e.target.value);
                  if (n !== null) setDraft({ ...draft, default_days: n });
                }}
              />
              days
            </label>
          </div>

          <div className="scheduler__group">
            <h3>By stage</h3>
            {stages.map((stage) => (
              <label key={stage} className="scheduler__row">
                {STAGE_LABEL(stage)}
                <input
                  type="number"
                  min={1}
                  max={90}
                  placeholder={String(draft.default_days)}
                  value={draft.by_stage[String(stage)] ?? ""}
                  onChange={(e) => setStage(stage, e.target.value)}
                />
                days
              </label>
            ))}
            <p className="scheduler__help">Leave blank to use the default.</p>
          </div>

          <div className="scheduler__group">
            <h3>By company</h3>
            {Object.entries(draft.by_company).map(([name, days]) => (
              <div key={name} className="scheduler__row">
                <span className="scheduler__company">{name}</span>
                <input
                  type="number"
                  min={1}
                  max={90}
                  value={days}
                  aria-label={`Days for ${name}`}
                  onChange={(e) => {
                    const n = parseDays(e.target.value);
                    if (n !== null) setCompany(name, n);
                  }}
                />
                days
                <button type="button" onClick={() => setCompany(name, null)} aria-label={`Remove rule for ${name}`}>
                  Remove
                </button>
              </div>
            ))}
            <div className="scheduler__row">
              <select value={newCompany} onChange={(e) => setNewCompany(e.target.value)} aria-label="Company for a new rule">
                <option value="">Choose a company…</option>
                {companies
                  .filter((c) => !(c.name in draft.by_company))
                  .map((c) => (
                    <option key={c.key} value={c.name}>
                      {c.name} ({c.count})
                    </option>
                  ))}
              </select>
              <input
                type="number"
                min={1}
                max={90}
                placeholder="days"
                value={newCompanyDays}
                onChange={(e) => setNewCompanyDays(e.target.value)}
                aria-label="Days for the new company rule"
              />
              <button type="button" onClick={addCompany} disabled={!newCompany || parseDays(newCompanyDays) === null}>
                Add rule
              </button>
            </div>
          </div>

          {error && <p className="error-text">{error}</p>}
          <div className="scheduler__footer">
            <button type="button" className="primary" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : "Save schedule"}
            </button>
            <button type="button" onClick={() => setDraft(saved)} disabled={!dirty || saving}>
              Discard changes
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
