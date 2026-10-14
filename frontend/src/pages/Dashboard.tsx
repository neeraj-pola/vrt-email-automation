import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Lead } from "../lib/types";
import { deriveStage, hasScheduledCall, initialSendDate, needsAttention, stageRank, stageTone } from "../lib/stage";
import {
  NO_FILTERS,
  applyColumnFilters,
  applyFilter,
  companyOptions,
  initials,
  qualifiedStatusLabel,
  type ColumnFilters,
  type LeadFilter,
} from "../lib/filters";
import { getPipelineConfig } from "../lib/config";
import FilterBar from "../components/FilterBar";
import SchedulePanel from "../components/SchedulePanel";
import Sidebar from "../components/Sidebar";
import { MobileTopBar, MobileTabBar, MobileSelectionBar } from "../components/MobileNav";
import LeadDetailPanel from "../components/LeadDetailPanel";
import ImportColumnMappingModal from "../components/ImportColumnMappingModal";
import { IconSearch } from "../components/icons";
import type { ImportPreviewResult } from "../lib/types";

type SortKey = "newest" | "oldest" | "recent_sent" | "least_recent_sent" | "recent_reply" | "stage" | "name" | "company";

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "recent_sent", label: "Recently sent" },
  { key: "least_recent_sent", label: "Least recently sent" },
  { key: "recent_reply", label: "Recently replied" },
  { key: "newest", label: "First contacted (newest)" },
  { key: "oldest", label: "First contacted (oldest)" },
  { key: "stage", label: "Stage (needs attention first)" },
  { key: "name", label: "Name (A–Z)" },
  { key: "company", label: "Company (A–Z)" },
];

// "Newest"/"Oldest" sort by the same date the date-range filter uses --
// initial-send date, not created_time (the ad-form fill date, which for a
// real lead has no relationship to when we actually contacted them).
// Sorting on a different date than the filter uses is exactly the kind of
// mismatch that made the date filter look broken. A lead never sent has no
// initial-send date and sorts to the end regardless of direction, rather
// than clustering at either end of a timeline it was never really on.
function compareByInitialSend(a: Lead, b: Lead, ascending: boolean): number {
  const da = initialSendDate(a);
  const db = initialSendDate(b);
  if (da === null && db === null) return 0;
  if (da === null) return 1;
  if (db === null) return -1;
  return ascending ? (da > db ? 1 : -1) : da < db ? 1 : -1;
}

// Orders by an ISO timestamp field; a lead without one (never sent / never
// replied) always sorts last, whichever direction, instead of clustering at
// one end of a timeline it was never on.
function compareByTimestamp(a: string | null, b: string | null, ascending: boolean): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (a === b) return 0;
  return ascending ? (a > b ? 1 : -1) : a < b ? 1 : -1;
}

const FILTER_NAMES: Record<LeadFilter, string> = {
  all: "All leads",
  needs_review: "Needs review",
  qualified: "Qualified",
  stopped: "Stopped",
};

export default function Dashboard() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<LeadFilter>("all");
  const [search, setSearch] = useState("");
  const [columnFilters, setColumnFilters] = useState<ColumnFilters>(NO_FILTERS);
  const [downloading, setDownloading] = useState(false);
  const [maxFollowUps, setMaxFollowUps] = useState(3);
  const [sort, setSort] = useState<SortKey>("recent_sent");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [openLeadId, setOpenLeadId] = useState<string | null>(null);
  // The dense list (leads, below) is fetched light -- no draft body/history,
  // the list never shows either -- but the detail panel needs both, so it's
  // fetched in full separately whenever a lead is opened, rather than reused
  // from the light list row. Starts from the light row (instant open, name/
  // status already visible) and is replaced once the full fetch resolves.
  const [openLeadFull, setOpenLeadFull] = useState<Lead | null>(null);
  const [importCandidate, setImportCandidate] = useState<{ file: File; preview: ImportPreviewResult } | null>(null);
  const [sendStats, setSendStats] = useState<{ sent_today: number; daily_cap: number } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const refreshSendStats = () => {
    api.getSendStats().then(setSendStats).catch(() => {});
  };

  // Live, not a one-time fetch: sends can happen from another tab, from
  // staff triggering a single follow-up from a lead's own panel, or from
  // the auto-tick's scheduled follow-ups -- polling is what keeps this
  // number honest without staff having to reload the page to see it move.
  useEffect(() => {
    refreshSendStats();
    const interval = setInterval(refreshSendStats, 30_000);
    return () => clearInterval(interval);
  }, []);

  const refresh = () => {
    setLoading(true);
    api
      .listLeads()
      .then(setLeads)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  useEffect(() => {
    getPipelineConfig().then((c) => setMaxFollowUps(c.max_follow_ups)).catch(() => {});
  }, []);

  // What the sidebar view alone leaves -- the company/stage/status/reply
  // dropdowns are built from (and counted against) this, so every number they
  // show matches what picking that option would actually leave on screen.
  const viewLeads = useMemo(() => applyFilter(leads, filter), [leads, filter]);

  const filtered = useMemo(() => {
    let result = applyColumnFilters(viewLeads, columnFilters, deriveStage);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter(
        (l) =>
          l.full_name.toLowerCase().includes(q) ||
          l.email.toLowerCase().includes(q) ||
          (l.company ?? "").toLowerCase().includes(q) ||
          (l.position ?? "").toLowerCase().includes(q) ||
          (l.location ?? "").toLowerCase().includes(q)
      );
    }
    // Filters by when the *initial outreach email* actually went out, not
    // created_time (when the lead filled out the ad form -- often months
    // before this system ever contacted them, so filtering on it just
    // returns nothing for a recent date range). initialSendDate is a full
    // ISO timestamp; the date inputs give a plain YYYY-MM-DD, so compare on
    // just the date portion. A lead never sent has no initial-send date and
    // is excluded whenever either bound is set.
    if (dateFrom || dateTo) {
      result = result.filter((l) => {
        const sent = initialSendDate(l);
        if (!sent) return false;
        const day = sent.slice(0, 10);
        return (!dateFrom || day >= dateFrom) && (!dateTo || day <= dateTo);
      });
    }
    const sorted = [...result];
    switch (sort) {
      case "oldest":
        sorted.sort((a, b) => compareByInitialSend(a, b, true));
        break;
      case "recent_sent":
        sorted.sort((a, b) => compareByTimestamp(a.last_sent_at, b.last_sent_at, false));
        break;
      case "least_recent_sent":
        sorted.sort((a, b) => compareByTimestamp(a.last_sent_at, b.last_sent_at, true));
        break;
      case "recent_reply":
        sorted.sort((a, b) => compareByTimestamp(a.last_reply_at, b.last_reply_at, false));
        break;
      case "company":
        sorted.sort((a, b) => (a.company ?? "\uffff").localeCompare(b.company ?? "\uffff") || a.full_name.localeCompare(b.full_name));
        break;
      case "stage":
        sorted.sort((a, b) => stageRank(a) - stageRank(b) || compareByInitialSend(a, b, false));
        break;
      case "name":
        sorted.sort((a, b) => a.full_name.localeCompare(b.full_name));
        break;
      default:
        sorted.sort((a, b) => compareByInitialSend(a, b, false));
    }
    return sorted;
  }, [viewLeads, columnFilters, search, sort, dateFrom, dateTo]);

  const openLeadLight = openLeadId ? leads.find((l) => l.id === openLeadId) ?? null : null;
  // Prefer the fully-fetched lead once it's in; fall back to the light row
  // so the panel opens instantly instead of blank while that fetch is in flight.
  const openLead = openLeadFull && openLeadFull.id === openLeadId ? openLeadFull : openLeadLight;

  useEffect(() => {
    if (!openLeadId) {
      setOpenLeadFull(null);
      return;
    }
    api.getLead(openLeadId).then(setOpenLeadFull).catch(() => {});
  }, [openLeadId]);

  const toggleSelected = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    setSelected((prev) => (prev.size === filtered.length ? new Set() : new Set(filtered.map((l) => l.id))));
  };

  // Click-drag multi-select across rows, plus shift-click for a range --
  // picking 60 leads out of a long list one checkbox at a time doesn't
  // scale. dragModeRef holds which way this drag is setting rows (based on
  // the first row's pre-drag state), so dragging back over already-touched
  // rows doesn't fight itself; lastClickedIdRef anchors shift-click ranges.
  const isDraggingRef = useRef(false);
  const dragModeRef = useRef(true);
  const lastClickedIdRef = useRef<string | null>(null);

  const setRowSelected = (id: string, value: boolean) => {
    setSelected((prev) => {
      if (prev.has(id) === value) return prev;
      const next = new Set(prev);
      if (value) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const handleCheckboxMouseDown = (id: string, currentlyChecked: boolean) => (e: React.MouseEvent) => {
    e.preventDefault(); // we drive selection state ourselves from here, not the checkbox's own click
    if (e.shiftKey && lastClickedIdRef.current) {
      const ids = filtered.map((l) => l.id);
      const a = ids.indexOf(lastClickedIdRef.current);
      const b = ids.indexOf(id);
      if (a !== -1 && b !== -1) {
        const [start, end] = a < b ? [a, b] : [b, a];
        setSelected((prev) => {
          const next = new Set(prev);
          for (let i = start; i <= end; i++) next.add(ids[i]);
          return next;
        });
      }
      lastClickedIdRef.current = id;
      return;
    }
    dragModeRef.current = !currentlyChecked;
    isDraggingRef.current = true;
    setRowSelected(id, dragModeRef.current);
    lastClickedIdRef.current = id;
  };

  const handleCheckboxMouseEnter = (id: string) => () => {
    if (isDraggingRef.current) setRowSelected(id, dragModeRef.current);
  };

  useEffect(() => {
    const stopDragging = () => {
      isDraggingRef.current = false;
    };
    window.addEventListener("mouseup", stopDragging);
    return () => window.removeEventListener("mouseup", stopDragging);
  }, []);

  // Picking a file no longer imports it immediately -- column naming isn't
  // guaranteed to stay identical across ad-platform exports (§3), so this
  // previews the file's actual headers and shows the mapping-confirmation
  // modal first. Nothing is imported until that's confirmed.
  const handleFilePicked = async (file: File) => {
    setBusy("Reading columns…");
    try {
      const preview = await api.previewImportColumns(file);
      setImportCandidate({ file, preview });
    } catch (e) {
      alert(`Couldn't read that file: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const handleConfirmImport = async (columnMap: Record<string, string>) => {
    if (!importCandidate) return;
    setBusy("Importing…");
    try {
      const result = await api.importLeads(importCandidate.file, columnMap);
      refresh();
      setImportCandidate(null);
      alert(`Imported ${result.imported} leads. Skipped ${result.skipped} (duplicates/invalid rows).`);
    } catch (e) {
      alert(`Import failed: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  // Downloads exactly what's on screen: the sidebar view, every dropdown, the
  // search box and the date range, in the current sort order -- so the file
  // can't drift from what staff were looking at when they clicked.
  const handleDownload = async () => {
    const parts: string[] = [FILTER_NAMES[filter]];
    if (columnFilters.company) parts.push(companyOptions(leads).find((c) => c.key === columnFilters.company)?.name ?? "company");
    if (columnFilters.stage) parts.push(columnFilters.stage);
    if (columnFilters.reply !== "all") parts.push(columnFilters.reply.replace("_", " "));
    if (columnFilters.status) parts.push(qualifiedStatusLabel(columnFilters.status));
    setDownloading(true);
    try {
      await api.exportLeads(filtered.map((l) => l.id), parts.join(" - "));
    } catch (e) {
      alert(`Download failed: ${e}`);
    } finally {
      setDownloading(false);
    }
  };

  const handleGenerateDrafts = async () => {
    if (selected.size === 0) return;
    setBusy(`Generating drafts for ${selected.size} leads…`);
    try {
      await api.generateDrafts([...selected]);
      refresh();
    } catch (e) {
      alert(`Draft generation failed: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const handleSendBatch = async () => {
    if (selected.size === 0) return;
    // "Send selected" always sends whatever's currently drafted for each
    // lead -- a first-timer's approved initial email, or an already-
    // contacted lead's pre-staged follow-up (send_current_draft on the
    // backend auto-detects which). The confirmation text used to always
    // say "initial outreach" regardless, which was actively misleading
    // once a selection included leads already mid-follow-up.
    const selectedLeads = leads.filter((l) => selected.has(l.id));
    const initialCount = selectedLeads.filter((l) => !l.last_sent_at).length;
    const followUpCount = selectedLeads.length - initialCount;
    const parts = [];
    if (initialCount > 0) parts.push(`${initialCount} initial email(s)`);
    if (followUpCount > 0) parts.push(`${followUpCount} follow-up(s)`);
    if (!confirm(`Send ${parts.join(" and ")}? Subject to the daily send cap.`)) return;
    setBusy("Sending…");
    try {
      const result = await api.sendBatch([...selected]);
      refresh();
      refreshSendStats();
      setSelected(new Set());
      alert(
        `Sent: ${result.sent.length}. Deferred to next batch (daily cap reached): ${result.deferred_to_next_batch.length}. Errors: ${result.errors.length}.`
      );
    } catch (e) {
      alert(`Send failed: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const handleTick = async () => {
    setBusy("Checking for replies / follow-ups…");
    try {
      const results = await api.tick();
      refresh();
      alert(`Check complete: ${results.length} lead(s) had new activity.`);
    } catch (e) {
      alert(`Tick failed: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const handleLeadUpdated = (updated: Lead) => {
    setLeads((prev) => prev.map((l) => (l.id === updated.id ? updated : l)));
    setOpenLeadFull((prev) => (prev && prev.id === updated.id ? updated : prev));
  };

  return (
    <div className="shell">
      <Sidebar leads={leads} active={filter} onSelect={setFilter} />
      <MobileTopBar
        search={search}
        onSearchChange={setSearch}
        sort={sort}
        onSortChange={(v) => setSort(v as SortKey)}
        sortOptions={SORT_OPTIONS}
        onCheckReplies={handleTick}
        onImportClick={() => fileInputRef.current?.click()}
        busy={!!busy}
      />

      <div className="main">
        <div className="topbar">
          <h1>Leads</h1>
          <div className="search">
            <IconSearch />
            <input
              type="text"
              placeholder="Search name, email, company…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <select
            className="sort-select"
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            aria-label="Sort leads by"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>
                {o.label}
              </option>
            ))}
          </select>
          <div className="date-filter" title="Filters by the date the initial outreach email was sent">
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              aria-label="Initial email sent from date"
              max={dateTo || undefined}
            />
            <span className="date-filter__sep">–</span>
            <input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              aria-label="Initial email sent to date"
              min={dateFrom || undefined}
            />
            {(dateFrom || dateTo) && (
              <button
                type="button"
                className="date-filter__clear"
                onClick={() => {
                  setDateFrom("");
                  setDateTo("");
                }}
                aria-label="Clear date filter"
              >
                ×
              </button>
            )}
          </div>
          {sendStats && (
            <div
              className={`send-stats ${sendStats.sent_today >= sendStats.daily_cap ? "send-stats--capped" : ""}`}
              title="Emails sent today across all leads, against the daily send cap (updates automatically)"
            >
              <span className="send-stats__dot" />
              {sendStats.sent_today} / {sendStats.daily_cap} sent today
            </div>
          )}
          <div className="spacer" />
          <button onClick={handleTick} disabled={!!busy}>
            Check replies
          </button>
          <button onClick={() => fileInputRef.current?.click()}>Import Excel</button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".xlsx,.xls"
            hidden
            onChange={(e) => {
              if (e.target.files?.[0]) handleFilePicked(e.target.files[0]);
              e.target.value = ""; // allow re-picking the same file later
            }}
          />
          <button onClick={handleGenerateDrafts} disabled={selected.size === 0 || !!busy}>
            Generate drafts ({selected.size})
          </button>
          <button onClick={handleSendBatch} disabled={selected.size === 0 || !!busy} className="primary">
            Send selected ({selected.size})
          </button>
        </div>

        <SchedulePanel leads={leads} maxFollowUps={maxFollowUps} />
        <FilterBar
          leads={viewLeads}
          filters={columnFilters}
          onChange={setColumnFilters}
          shownCount={filtered.length}
          onDownload={handleDownload}
          downloading={downloading}
        />

        {busy && <div className="busy-banner">{busy}</div>}
        {error && <div className="error-banner">{error}</div>}

        <div className="lead-table-wrap">
          <table className="lead-table">
            <thead>
              <tr>
                <th className="checkbox-col">
                  <input
                    type="checkbox"
                    checked={selected.size === filtered.length && filtered.length > 0}
                    onChange={toggleSelectAll}
                  />
                </th>
                <th>Name</th>
                <th>Company</th>
                <th>Position</th>
                <th>Employees</th>
                <th>Location</th>
                <th>Status</th>
                <th>Stage</th>
                <th>Reply</th>
                <th>Initial email sent</th>
                <th>Last activity</th>
                <th>Scheduled call</th>
                <th>Website clicked</th>
                <th>Get started clicked</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={14} className="empty-state">
                    Loading…
                  </td>
                </tr>
              )}
              {!loading && filtered.length === 0 && (
                <tr>
                  <td colSpan={14} className="empty-state">
                    No leads match this view.
                  </td>
                </tr>
              )}
              {filtered.map((lead) => {
                const attention = needsAttention(lead);
                const scheduled = hasScheduledCall(lead);
                const initialSent = initialSendDate(lead);
                const rowClass = [
                  selected.has(lead.id) && "row-selected",
                  (attention || scheduled) && "row-attention",
                  scheduled && "row-call",
                ]
                  .filter(Boolean)
                  .join(" ");
                return (
                  <tr
                    key={lead.id}
                    className={`row-clickable ${rowClass}`}
                    onClick={() => setOpenLeadId(lead.id)}
                    tabIndex={0}
                    role="button"
                    aria-label={`Open ${lead.full_name}`}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setOpenLeadId(lead.id);
                      }
                    }}
                  >
                    <td
                      className="checkbox-col"
                      onClick={(e) => e.stopPropagation()}
                      onMouseDown={handleCheckboxMouseDown(lead.id, selected.has(lead.id))}
                      onMouseEnter={handleCheckboxMouseEnter(lead.id)}
                    >
                      <input type="checkbox" checked={selected.has(lead.id)} onChange={() => {}} />
                    </td>
                    <td>
                      <div className="leadcell">
                        <div className="avatar">{initials(lead.full_name)}</div>
                        <div className="leadcell__meta">
                          <span className="lead-name">{lead.full_name}</span>
                          <div className="lead-email">{lead.email}</div>
                        </div>
                      </div>
                    </td>
                    <td className="source-col">{lead.company ?? "—"}</td>
                    <td className="source-col">{lead.position ?? "—"}</td>
                    <td className="phone-col">{lead.employee_count ?? "—"}</td>
                    <td className="source-col">{lead.location ?? "—"}</td>
                    <td>
                      <span className={`badge badge-${lead.qualified_status}`}>{qualifiedStatusLabel(lead.qualified_status)}</span>
                    </td>
                    <td>
                      <span
                        className={`stage-pill stage-${stageTone(lead)} ${
                          attention || scheduled ? "stage-pill--attention" : ""
                        }`}
                      >
                        {deriveStage(lead)}
                      </span>
                    </td>
                    <td className="reply-col">
                      {lead.last_reply_text ? (
                        <>
                          <div className="reply-col__text" title={lead.last_reply_text}>
                            {lead.last_reply_text}
                          </div>
                          <div className="source-sub">{lead.ai_response_classification ?? ""}</div>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{initialSent ? new Date(initialSent).toLocaleDateString() : "—"}</td>
                    <td>{lead.last_sent_at ? new Date(lead.last_sent_at).toLocaleDateString() : "—"}</td>
                    <td className={scheduled ? "call-time" : ""}>
                      {lead.scheduled_call_time ? new Date(lead.scheduled_call_time).toLocaleString() : "—"}
                    </td>
                    <td className={lead.website_clicked_at ? "call-time" : ""}>
                      {lead.website_clicked_at ? new Date(lead.website_clicked_at).toLocaleString() : "—"}
                    </td>
                    <td className={lead.get_started_clicked_at ? "call-time" : ""}>
                      {lead.get_started_clicked_at ? new Date(lead.get_started_clicked_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* ---- Mobile card list: same data as the table above, laid out
             for a phone instead of horizontal columns. Labeled fields
             (Status/Source) so nothing depends on memorizing a color, plus
             a left-edge stripe carrying urgency at a glance -- same idea as
             the desktop table's row-attention treatment. ---- */}
        <div className="mobile-lead-list">
          {loading && <div className="empty-state">Loading…</div>}
          {!loading && filtered.length === 0 && <div className="empty-state">No leads match this view.</div>}
          {filtered.map((lead) => {
            const attention = needsAttention(lead);
            const scheduled = hasScheduledCall(lead);
            const cardClass = [
              selected.has(lead.id) && "is-selected",
              scheduled ? "is-call" : attention ? "is-attn" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <div key={lead.id} className={`mobile-lead-card ${cardClass}`} onClick={() => setOpenLeadId(lead.id)}>
                <div className="mobile-lead-card__head">
                  <button
                    className={`mobile-lead-card__check ${selected.has(lead.id) ? "is-checked" : ""}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleSelected(lead.id);
                    }}
                    aria-label={`Select ${lead.full_name}`}
                  >
                    ✓
                  </button>
                  <div className="avatar">{initials(lead.full_name)}</div>
                  <div className="mobile-lead-card__id">
                    <span className="lead-name">{lead.full_name}</span>
                    <div className="lead-email">{lead.email}</div>
                  </div>
                </div>
                <div className="mobile-lead-card__grid">
                  <div>
                    <div className="mobile-lead-card__label">Status</div>
                    <span
                      className={`stage-pill stage-${stageTone(lead)} ${
                        attention || scheduled ? "stage-pill--attention" : ""
                      }`}
                    >
                      {deriveStage(lead)}
                    </span>
                  </div>
                  <div>
                    <div className="mobile-lead-card__label">Company</div>
                    <span className="badge badge-unset">{lead.company ?? "—"}</span>
                  </div>
                  <div>
                    <div className="mobile-lead-card__label">Employees</div>
                    <span className="badge badge-unset">{lead.employee_count ?? "—"}</span>
                  </div>
                </div>
                {scheduled && (
                  <div className="mobile-lead-card__call">
                    📞 Call scheduled — {new Date(lead.scheduled_call_time!).toLocaleString()}
                  </div>
                )}
                {lead.website_clicked_at && (
                  <div className="mobile-lead-card__call">
                    🔗 Website clicked — {new Date(lead.website_clicked_at).toLocaleString()}
                  </div>
                )}
                {lead.get_started_clicked_at && (
                  <div className="mobile-lead-card__call">
                    🔗 Get started clicked — {new Date(lead.get_started_clicked_at).toLocaleString()}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <MobileSelectionBar
        count={selected.size}
        onGenerateDrafts={handleGenerateDrafts}
        onSendSelected={handleSendBatch}
        onClear={() => setSelected(new Set())}
        busy={!!busy}
      />
      <MobileTabBar leads={leads} active={filter} onSelect={setFilter} />

      {openLead && (
        <LeadDetailPanel lead={openLead} onClose={() => setOpenLeadId(null)} onUpdate={handleLeadUpdated} />
      )}

      {importCandidate && (
        <ImportColumnMappingModal
          file={importCandidate.file}
          preview={importCandidate.preview}
          onCancel={() => setImportCandidate(null)}
          onConfirm={handleConfirmImport}
          importing={!!busy}
        />
      )}
    </div>
  );
}
