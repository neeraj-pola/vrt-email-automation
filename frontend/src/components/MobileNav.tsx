import { useState } from "react";
import { Link } from "react-router-dom";
import { FILTERS, filterCounts, type LeadFilter } from "../lib/filters";
import type { Lead } from "../lib/types";
import { IconAll, IconAnalytics, IconClose, IconImport, IconModifyDraft, IconMore, IconQualified, IconRefresh, IconReview, IconSearch, IconStopped } from "./icons";

const ICONS: Record<LeadFilter, () => React.JSX.Element> = {
  all: IconAll,
  needs_review: IconReview,
  qualified: IconQualified,
  stopped: IconStopped,
};

interface SortOption {
  key: string;
  label: string;
}

interface MobileTopBarProps {
  search: string;
  onSearchChange: (v: string) => void;
  sort: string;
  onSortChange: (v: string) => void;
  sortOptions: SortOption[];
  onCheckReplies: () => void;
  onImportClick: () => void;
  busy: boolean;
}

// Everyday-but-not-constant actions (check replies, import, sort) tuck
// behind a single "more" button rather than sitting permanently in the
// topbar -- there's only room for one or two icons on a phone width before
// it starts wrapping into multiple rows, which is what broke this before.
// Bulk actions (generate drafts / send) aren't here at all -- see
// MobileSelectionBar, which only appears once a selection makes them mean
// anything.
export function MobileTopBar({
  search,
  onSearchChange,
  sort,
  onSortChange,
  sortOptions,
  onCheckReplies,
  onImportClick,
  busy,
}: MobileTopBarProps) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  if (searchOpen) {
    return (
      <div className="mobile-topbar">
        <div className="mobile-topbar__search">
          <IconSearch />
          <input
            autoFocus
            type="text"
            placeholder="Search name, email, company…"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
          />
        </div>
        <button
          className="mobile-topbar__btn"
          aria-label="Close search"
          onClick={() => {
            setSearchOpen(false);
            onSearchChange("");
          }}
        >
          <IconClose />
        </button>
      </div>
    );
  }

  return (
    <div className="mobile-topbar">
      <div className="mobile-topbar__title"><img src="/logo.png" alt="VRT Management Group" className="mobile-topbar__logo" /></div>
      <button className="mobile-topbar__btn" aria-label="Search" onClick={() => setSearchOpen(true)}>
        <IconSearch />
      </button>
      <div className="mobile-topbar__menu-wrap">
        <button
          className="mobile-topbar__btn"
          aria-label="More actions"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((v) => !v)}
        >
          <IconMore />
        </button>
        {menuOpen && (
          <>
            <div className="mobile-menu__backdrop" onClick={() => setMenuOpen(false)} />
            <div className="mobile-menu">
              <button
                disabled={busy}
                onClick={() => {
                  setMenuOpen(false);
                  onCheckReplies();
                }}
              >
                <IconRefresh /> Check replies
              </button>
              <button
                onClick={() => {
                  setMenuOpen(false);
                  onImportClick();
                }}
              >
                <IconImport /> Import Excel
              </button>
              <Link to="/analytics" onClick={() => setMenuOpen(false)}>
                <IconAnalytics /> Analytics
              </Link>
              <Link to="/modify-draft" onClick={() => setMenuOpen(false)}>
                <IconModifyDraft /> Modify draft
              </Link>
              <label className="mobile-menu__sort">
                Sort by
                <select value={sort} onChange={(e) => onSortChange(e.target.value)}>
                  {sortOptions.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

interface TabBarProps {
  leads: Lead[];
  active: LeadFilter;
  onSelect: (f: LeadFilter) => void;
}

export function MobileTabBar({ leads, active, onSelect }: TabBarProps) {
  const counts = filterCounts(leads);
  return (
    <nav className="mobile-tabbar">
      {FILTERS.map((f) => {
        const Icon = ICONS[f.key];
        return (
          <button
            key={f.key}
            className={`navitem ${active === f.key ? "is-active" : ""}`}
            onClick={() => onSelect(f.key)}
          >
            <Icon />
            {f.label.replace("Needs ", "")}
            <span className="navitem__count">{counts[f.key]}</span>
          </button>
        );
      })}
    </nav>
  );
}

interface SelectionBarProps {
  count: number;
  onGenerateDrafts: () => void;
  onSendSelected: () => void;
  onClear: () => void;
  busy: boolean;
}

// Bulk actions only ever apply to a selection, so they only exist once one
// is made -- same pattern as Gmail's mobile app. Sits fixed just above the
// tab bar so it reads as "attached to the list," not a random floating bar.
export function MobileSelectionBar({ count, onGenerateDrafts, onSendSelected, onClear, busy }: SelectionBarProps) {
  if (count === 0) return null;
  return (
    <div className="mobile-selection-bar">
      <button className="mobile-selection-bar__x" aria-label="Clear selection" onClick={onClear}>
        <IconClose />
      </button>
      <div className="mobile-selection-bar__count">{count} selected</div>
      <button disabled={busy} onClick={onGenerateDrafts}>
        Generate drafts
      </button>
      <button disabled={busy} className="primary" onClick={onSendSelected}>
        Send
      </button>
    </div>
  );
}
