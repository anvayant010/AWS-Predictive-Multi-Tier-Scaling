import type { EpisodeView } from "../lib/episode";
import { counters, laneRows, narrate, type CellState, type LaneRow, type Side } from "../lib/lanes";

interface Props {
  view: EpisodeView;
  now: number;
  readyDelayMinutes: number;
}

const GLYPH: Partial<Record<CellState, string>> = { over: "!", saturated: "!", cold: "!" };

const STATE_NAME: Record<CellState, string> = {
  off: "not running",
  booting: "starting",
  healthy: "serving",
  over: "serving, over plan",
  saturated: "serving, saturated",
  provisioned: "ready",
  provisioning: "being set up",
  warm: "warm on demand",
  cold: "cold start",
};

function describeRow(row: LaneRow): string {
  const counts = new Map<CellState, number>();
  for (const cell of row.cells) if (cell !== "off") counts.set(cell, (counts.get(cell) ?? 0) + 1);
  const parts = [...counts].map(([state, n]) => `${n} ${STATE_NAME[state]}`);
  if (row.overflow > 0) parts.push(`${row.overflow} more not shown`);
  return parts.length > 0 ? parts.join(", ") : "none";
}

function Row({ row, who }: { row: LaneRow; who: string }) {
  return (
    <div className="lane-row">
      {row.label && <span className="lane-row-label">{row.label}</span>}
      <div className="cells" role="img" aria-label={`${who}${row.label ? `, ${row.label.toLowerCase()}` : ""}: ${describeRow(row)}`}>
        {row.cells.map((state, i) => (
          <span key={i} className={`cell ${state}`}>
            {GLYPH[state]}
          </span>
        ))}
        {row.overflow > 0 && <span className="cells-more mono">+{row.overflow}</span>}
      </div>
    </div>
  );
}

function Lane({ view, side, now, readyDelayMinutes }: Props & { side: Side }) {
  const arm = view[side];
  const color = side === "predictive" ? "var(--predictive)" : "var(--reactive)";
  const tagline = side === "predictive" ? "reads the forecast, asks early" : "waits for the load, then asks";
  return (
    <div className={`lane ${side}`} style={{ ["--lane" as string]: color }}>
      <div className="lane-head">
        <span className="lane-name">
          <i className="key" style={{ background: color }} aria-hidden="true" />
          <b>{arm.label}</b>
          <span className="lane-tag">{tagline}</span>
        </span>
        <div className="lane-counters mono">
          {counters(view, side, now).map((c) => (
            <span key={c.label}>
              <b>{c.value}</b>
              <small>{c.label}</small>
            </span>
          ))}
        </div>
      </div>
      {laneRows(view, side, now).map((row) => (
        <Row key={row.label || "row"} row={row} who={arm.label} />
      ))}
      <p className="narration">{narrate(view, side, now, readyDelayMinutes)}</p>
    </div>
  );
}

function CellKey({ view }: { view: EpisodeView }) {
  const items: [CellState, string][] =
    view.faultKind === "over_plan"
      ? [
          ["healthy", "Serving"],
          ["booting", "Starting"],
          ["over", "Serving, traffic over the plan"],
          ["off", "Not running"],
        ]
      : [
          ["provisioned", "Provisioned, ready"],
          ["provisioning", "Being set up"],
          ["warm", "On demand, warm"],
          ["cold", "Cold start"],
          ["off", "Empty"],
        ];
  return (
    <ul className="cell-key" aria-label="What the squares mean">
      {items.map(([state, label]) => (
        <li key={state}>
          <span className={`cell ${state} mini`}>{GLYPH[state]}</span>
          {label}
        </li>
      ))}
    </ul>
  );
}

/** Two lanes of squares, one per system, with running totals and a plain-English line about the latest decision. */
export function Lanes({ view, now, readyDelayMinutes }: Props) {
  return (
    <div className="lanes-wrap">
      <div className="lanes">
        <Lane view={view} now={now} side="predictive" readyDelayMinutes={readyDelayMinutes} />
        <Lane view={view} now={now} side="reactive" readyDelayMinutes={readyDelayMinutes} />
      </div>
      <CellKey view={view} />
    </div>
  );
}
