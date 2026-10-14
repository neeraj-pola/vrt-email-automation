import { NavLink } from "react-router-dom";
import { FILTERS, filterCounts, type LeadFilter } from "../lib/filters";
import type { Lead } from "../lib/types";
import { IconAll, IconAnalytics, IconModifyDraft, IconQualified, IconReview, IconStopped } from "./icons";
import { useAuth } from "../context/AuthContext";

const ICONS: Record<LeadFilter, () => React.JSX.Element> = {
  all: IconAll,
  needs_review: IconReview,
  qualified: IconQualified,
  stopped: IconStopped,
};

interface Props {
  leads: Lead[];
  active: LeadFilter;
  onSelect: (f: LeadFilter) => void;
}

export default function Sidebar({ leads, active, onSelect }: Props) {
  const { signOut, authEnabled, session } = useAuth();
  const counts = filterCounts(leads);

  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <img src="/logo.png" alt="VRT Management Group" className="sidebar__logo" />
      </div>
      <nav className="sidebar__nav">
        {FILTERS.map((f) => {
          const Icon = ICONS[f.key];
          return (
            <button
              key={f.key}
              className={`navitem ${active === f.key ? "is-active" : ""}`}
              onClick={() => onSelect(f.key)}
            >
              <Icon />
              {f.label}
              <span className="navitem__count">{counts[f.key]}</span>
            </button>
          );
        })}
        <NavLink to="/analytics" className={({ isActive }) => `navitem ${isActive ? "is-active" : ""}`}>
          <IconAnalytics />
          Analytics
        </NavLink>
        <NavLink to="/modify-draft" className={({ isActive }) => `navitem ${isActive ? "is-active" : ""}`}>
          <IconModifyDraft />
          Modify draft
        </NavLink>
      </nav>
      <div className="sidebar__spacer" />
      {authEnabled && (
        <div className="sidebar__user">
          <div className="avatar">{(session?.user.email ?? "ST").slice(0, 2).toUpperCase()}</div>
          <div className="sidebar__user-name">{session?.user.email ?? "Staff"}</div>
          <button className="sidebar__signout" onClick={signOut}>
            Sign out
          </button>
        </div>
      )}
    </aside>
  );
}
