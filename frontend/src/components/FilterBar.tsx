import type { Lead, QualifiedStatus } from "../lib/types";
import {
  NO_FILTERS,
  REPLY_LABELS,
  activeFilterCount,
  companyOptions,
  qualifiedStatusLabel,
  type ColumnFilters,
  type ReplyFilter,
} from "../lib/filters";
import { deriveStage, stageRank } from "../lib/stage";

interface Props {
  // The leads this bar narrows -- already limited to the active sidebar view,
  // so every count shown matches what picking that option would leave.
  leads: Lead[];
  filters: ColumnFilters;
  onChange: (next: ColumnFilters) => void;
  shownCount: number;
  onDownload: () => void;
  downloading: boolean;
}

const STATUSES: QualifiedStatus[] = ["unset", "qualified", "not_qualified"];

export default function FilterBar({ leads, filters, onChange, shownCount, onDownload, downloading }: Props) {
  const companies = companyOptions(leads);

  const stageCounts = new Map<string, { lead: Lead; count: number }>();
  for (const l of leads) {
    const stage = deriveStage(l);
    const entry = stageCounts.get(stage);
    if (entry) entry.count += 1;
    else stageCounts.set(stage, { lead: l, count: 1 });
  }
  const stages = [...stageCounts.entries()].sort((a, b) => stageRank(a[1].lead) - stageRank(b[1].lead));

  const count = (pred: (l: Lead) => boolean) => leads.filter(pred).length;
  const active = activeFilterCount(filters);

  return (
    <div className="filterbar" role="group" aria-label="Filter leads">
      <label className="filterbar__field">
        Company
        <select value={filters.company} onChange={(e) => onChange({ ...filters, company: e.target.value })}>
          <option value="">All companies ({leads.length})</option>
          {companies.map((c) => (
            <option key={c.key} value={c.key}>
              {c.name} ({c.count})
            </option>
          ))}
        </select>
      </label>

      <label className="filterbar__field">
        Stage
        <select value={filters.stage} onChange={(e) => onChange({ ...filters, stage: e.target.value })}>
          <option value="">All stages</option>
          {stages.map(([stage, { count: n }]) => (
            <option key={stage} value={stage}>
              {stage} ({n})
            </option>
          ))}
        </select>
      </label>

      <label className="filterbar__field">
        Reply
        <select value={filters.reply} onChange={(e) => onChange({ ...filters, reply: e.target.value as ReplyFilter })}>
          <option value="all">Any reply</option>
          {(Object.keys(REPLY_LABELS) as Exclude<ReplyFilter, "all">[]).map((key) => (
            <option key={key} value={key}>
              {REPLY_LABELS[key]} (
              {count((l) => {
                const ai = l.ai_response_classification;
                return key === "none" ? !ai || ai === "pending" : ai === key;
              })}
              )
            </option>
          ))}
        </select>
      </label>

      <label className="filterbar__field">
        Status
        <select
          value={filters.status}
          onChange={(e) => onChange({ ...filters, status: e.target.value as QualifiedStatus | "" })}
        >
          <option value="">Any status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {qualifiedStatusLabel(s)} ({count((l) => l.qualified_status === s)})
            </option>
          ))}
        </select>
      </label>

      <div className="filterbar__actions">
        {active > 0 && (
          <button type="button" className="filterbar__clear" onClick={() => onChange(NO_FILTERS)}>
            Clear filters ({active})
          </button>
        )}
        <button
          type="button"
          onClick={onDownload}
          disabled={downloading || shownCount === 0}
          title="Downloads exactly the leads shown below, with all the filters applied"
        >
          {downloading ? "Preparing…" : `Download Excel (${shownCount})`}
        </button>
      </div>
    </div>
  );
}
