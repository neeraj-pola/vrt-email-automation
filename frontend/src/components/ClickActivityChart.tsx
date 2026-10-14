import { useMemo, useState } from "react";
import type { Lead } from "../lib/types";

// Categorical pair: brand rust vs. teal -- clearly distinct in hue and
// lightness on the white surface. NOT yet re-run through the dataviz
// skill's palette validator after the VRT retheme. Hardcoded here rather
// than read from the CSS custom properties (--chart-website /
// --chart-get-started, defined in index.css for the legend swatches)
// because an SVG fill attribute can't reliably resolve var() the way a
// real CSS property can.
const COLORS = { website: "#cc1b1b", get_started: "#1f7a80" } as const;
const LABELS = { website: "Website", get_started: "Get started" } as const;
const DAY_MS = 86400000;

// Company is in Botsford, CT -- same fix as backend/app/pipeline/
// send_batch.py's leads_sent_today. A click late in the actual business
// evening (e.g. 11:41 PM ET) has a UTC timestamp already past midnight,
// on the *next* UTC calendar date -- bucketing by iso.slice(0, 10) (the
// old approach) silently plotted it a day later than it really happened.
// Confirmed live 2026-09-17: 4 of 5 checked clicks were off by a day.
const COMPANY_TZ = "America/New_York";

type ClickType = "website" | "get_started";

interface DayBucket {
  dayMs: number;
  website: string[];
  get_started: string[];
}

interface HoverInfo {
  x: number;
  y: number;
  dayLabel: string;
  type: ClickType;
  names: string[];
}

function dayKey(iso: string): string {
  // "en-CA" formats as YYYY-MM-DD. Explicit timeZone is what actually
  // fixes the bug -- without it this uses the browser's own local zone,
  // which is only right by accident for a viewer who happens to also be
  // in Eastern time.
  return new Date(iso).toLocaleDateString("en-CA", { timeZone: COMPANY_TZ });
}

function keyToMs(key: string): number {
  // Anchored at noon UTC on the Y-M-D the key names -- not a real
  // "moment," just a stable, evenly-spaced value to sort/position by.
  // Built from explicit numeric parts (not `new Date(key)`) to sidestep
  // the ECMAScript quirk where a date-only string parses as UTC midnight.
  const [y, m, d] = key.split("-").map(Number);
  return Date.UTC(y, m - 1, d, 12);
}

function formatDay(ms: number): string {
  // Explicit timeZone here too -- otherwise the noon-UTC anchor above,
  // shown in the browser's own local zone, silently un-fixes the bucketing
  // fix the moment it's displayed to a viewer outside a few hours of UTC.
  return new Date(ms).toLocaleDateString([], { month: "short", day: "numeric", timeZone: COMPANY_TZ });
}

// 4px-radius top corners, square baseline (marks-and-anatomy.md's bar
// spec) -- an SVG <rect> can't round only two corners, so the bar is drawn
// as an explicit path instead. Falls back to a plain rect once the bar is
// shorter than the radius (a count of 1 can be a very short bar at a tall
// y-scale), so the curve never exceeds the bar's own height.
function barPath(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.min(r, h, w / 2);
  if (rr <= 0) return `M${x},${y} h${w} v${h} h${-w} Z`;
  return `M${x},${y + h} V${y + rr} Q${x},${y} ${x + rr},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} V${y + h} Z`;
}

export default function ClickActivityChart({ leads }: { leads: Lead[] }) {
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);

  const { buckets, rangeStart, rangeEnd, maxCount, dayCount } = useMemo(() => {
    const byDay = new Map<string, DayBucket>();
    leads.forEach((l) => {
      if (l.website_clicked_at) {
        const key = dayKey(l.website_clicked_at);
        const bucket = byDay.get(key) ?? { dayMs: keyToMs(key), website: [], get_started: [] };
        bucket.website.push(l.full_name);
        byDay.set(key, bucket);
      }
      if (l.get_started_clicked_at) {
        const key = dayKey(l.get_started_clicked_at);
        const bucket = byDay.get(key) ?? { dayMs: keyToMs(key), website: [], get_started: [] };
        bucket.get_started.push(l.full_name);
        byDay.set(key, bucket);
      }
    });
    const sorted = [...byDay.values()].sort((a, b) => a.dayMs - b.dayMs);
    const start = sorted.length ? sorted[0].dayMs : Date.now();
    const end = sorted.length ? sorted[sorted.length - 1].dayMs : Date.now();
    const max = Math.max(1, ...sorted.flatMap((b) => [b.website.length, b.get_started.length]));
    const days = Math.round((end - start) / DAY_MS) + 1;
    return { buckets: sorted, rangeStart: start, rangeEnd: Math.max(end, start + DAY_MS), maxCount: max, dayCount: Math.max(days, 1) };
  }, [leads]);

  if (buckets.length === 0) {
    return (
      <div className="qualbox chart-card">
        <h3>Link clicks per day</h3>
        <p className="chatbot-hint">No clicks yet — this fills in once leads start clicking email links.</p>
      </div>
    );
  }

  const width = 900;
  const height = 260;
  const margin = { left: 40, right: 20, top: 16, bottom: 34 };
  const plotWidth = width - margin.left - margin.right;
  const yTop = Math.ceil(maxCount) + 1;

  const slotWidth = plotWidth / dayCount;
  // Capped at 24px per the bar spec, but also shrinks to fit a dense date
  // range rather than overflowing its day slot -- the fix for the
  // overlapping-dots problem this replaces: bars have a real footprint and
  // a surface gap between them, so two series on the same day never sit on
  // top of each other the way same-height dots could.
  const barWidth = Math.max(2, Math.min(24, (slotWidth - 6) / 2));
  const xForDay = (dayMs: number) => margin.left + ((dayMs - rangeStart) / DAY_MS) * slotWidth;
  const yFor = (count: number) => {
    const plotTop = margin.top;
    const plotBottom = height - margin.bottom;
    return plotBottom - (count / yTop) * (plotBottom - plotTop);
  };
  const baseline = height - margin.bottom;

  const yTicks = Array.from({ length: yTop + 1 }, (_, i) => i);

  // Thin the x-axis to roughly 8-10 labels regardless of range length --
  // one label per day is what made the old axis illegible past about two
  // weeks of data. Gridline ticks stay at the same reduced cadence, since
  // a label-only thin would leave orphaned gridlines with no date to
  // anchor to.
  const targetLabels = 9;
  const step = Math.max(1, Math.round(dayCount / targetLabels));
  const dayTicks: number[] = [];
  for (let i = 0; i < dayCount; i += step) dayTicks.push(rangeStart + i * DAY_MS);
  const lastTick = dayTicks[dayTicks.length - 1];
  const finalDay = rangeEnd - DAY_MS;
  // Show the last day too, so the range never visually trails off -- but
  // only if it's far enough from the previous label to avoid the exact
  // collision this is meant to fix (a short final gap otherwise overlapped,
  // e.g. "Sep 28"/"Sep 29" landing back to back).
  if (lastTick === undefined || finalDay - lastTick >= (step * DAY_MS) / 2) {
    dayTicks.push(finalDay);
  }

  const bars: { key: string; type: ClickType; x: number; y: number; w: number; h: number; dayLabel: string; names: string[] }[] = [];
  buckets.forEach((b) => {
    (["website", "get_started"] as ClickType[]).forEach((type, i) => {
      const names = b[type];
      if (names.length === 0) return;
      const slotCenter = xForDay(b.dayMs) + slotWidth / 2;
      const x = slotCenter + (i === 0 ? -barWidth - 1 : 1);
      const y = yFor(names.length);
      bars.push({ key: `${b.dayMs}-${type}`, type, x, y, w: barWidth, h: baseline - y, dayLabel: formatDay(b.dayMs), names });
    });
  });

  return (
    <div className="qualbox chart-card">
      <h3>Link clicks per day</h3>
      <p className="range">
        {formatDay(rangeStart)} – {formatDay(rangeEnd - DAY_MS)}
      </p>
      <div className="legend">
        <div className="legend-item">
          <span className="legend-swatch legend-swatch--bar" style={{ background: COLORS.website }} />
          Website
        </div>
        <div className="legend-item">
          <span className="legend-swatch legend-swatch--bar" style={{ background: COLORS.get_started }} />
          Get started
        </div>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet">
        {yTicks.map((c) => {
          const y = yFor(c);
          return (
            <g key={c}>
              <line className="gridline" x1={margin.left} y1={y} x2={width - margin.right} y2={y} />
              <text className="axis-label" x={margin.left - 10} y={y + 4} textAnchor="end">
                {c}
              </text>
            </g>
          );
        })}
        {dayTicks.map((t) => (
          <text key={t} className="axis-label" x={xForDay(t) + slotWidth / 2} y={height - 12} textAnchor="middle">
            {formatDay(t)}
          </text>
        ))}
        {bars.map((bar) => (
          <path
            key={bar.key}
            className={`bar${hoveredKey === bar.key ? " bar--hover" : ""}`}
            d={barPath(bar.x, bar.y, bar.w, bar.h, 4)}
            fill={COLORS[bar.type]}
            onPointerEnter={(e) => {
              setHoveredKey(bar.key);
              setHover({ x: e.clientX, y: e.clientY, dayLabel: bar.dayLabel, type: bar.type, names: bar.names });
            }}
            onPointerMove={(e) => setHover((h) => (h ? { ...h, x: e.clientX, y: e.clientY } : h))}
            onPointerLeave={() => {
              setHoveredKey(null);
              setHover(null);
            }}
          />
        ))}
      </svg>

      {hover && (
        <div className="chart-tooltip" style={{ left: hover.x + 14, top: hover.y + 14 }}>
          <div className="chart-tooltip__value">
            {hover.names.length} {LABELS[hover.type].toLowerCase()} click{hover.names.length === 1 ? "" : "s"}
          </div>
          <div className="chart-tooltip__label">
            {hover.dayLabel}
            <br />
            {hover.names.join(", ")}
          </div>
        </div>
      )}
    </div>
  );
}
