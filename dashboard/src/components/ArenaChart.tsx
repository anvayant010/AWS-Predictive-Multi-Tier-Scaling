import { memo, useMemo } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { chartRows, type ChartRow, type EpisodeView, yAxisTicks, yCeiling } from "../lib/episode";
import { useRootScale } from "../hooks/useRootScale";
import { clockLabel } from "../lib/format";

/** Plot-area insets in px at the 16px base size; they grow with the page. The fault strips under the chart use the same numbers (as rem) to line up with it. */
export const PLOT_LEFT = 46;
export const PLOT_RIGHT = 12;
export const rem = (px: number): string => `${px / 16}rem`;

interface Props {
  view: EpisodeView;
  now: number;
  onScrub: (minute: number) => void;
}

const fmt = (value: number, digits = 1) => (Math.abs(value - Math.round(value)) < 0.05 ? String(Math.round(value)) : value.toFixed(digits));

function ArenaTooltip({ active, payload, view }: { active?: boolean; payload?: { payload: ChartRow }[]; view: EpisodeView }) {
  const row = active ? payload?.[0]?.payload : undefined;
  if (!row || row.need === null) return null;
  const unit = view.unit.many;
  return (
    <div className="tip" role="status">
      <div className="tip-time">{clockLabel(view.episode.start, row.minute)}</div>
      <div className="tip-row">
        <i className="tip-key" style={{ background: "var(--actual)" }} />
        <span>Needed</span>
        <b>{fmt(row.need)}</b>
      </div>
      <div className="tip-row">
        <i className="tip-key" style={{ background: "var(--predictive)" }} />
        <span>Predictive has</span>
        <b>{fmt(row.predictive ?? 0)}</b>
      </div>
      <div className="tip-row">
        <i className="tip-key" style={{ background: "var(--reactive)" }} />
        <span>Reactive has</span>
        <b>{fmt(row.reactive ?? 0)}</b>
      </div>
      <div className="tip-unit">{unit}, ready to serve</div>
    </div>
  );
}

function AskDot({ cx, cy, fill, scale }: { cx?: number; cy?: number; fill: string; scale: number }) {
  if (cx === undefined || cy === undefined) return null;
  return <circle cx={cx} cy={cy} r={5 * scale} fill={fill} stroke="var(--bg)" strokeWidth={2} />;
}

function ArenaChartImpl({ view, now, onScrub }: Props) {
  const scale = useRootScale();
  const text = { fill: "var(--ink-3)", fontSize: 13 * scale, fontFamily: "var(--mono)" };
  const rows = useMemo(() => chartRows(view, now), [view, now]);
  const axis = useMemo(() => yAxisTicks(yCeiling(view)), [view]);
  const last = view.episode.minutes - 1;
  const ticks = useMemo(() => {
    const out: number[] = [];
    const every = last > 90 ? 30 : 10;
    for (let m = 0; m <= last; m += every) out.push(m);
    return out;
  }, [last]);

  return (
    <div className="arena-chart" role="img" aria-label={`Chart of ${view.episode.title}: capacity needed, and what each system had ready, minute by minute, with the model's forecast ahead of now.`}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={rows}
          margin={{ top: 14 * scale, right: PLOT_RIGHT * scale, bottom: 4 * scale, left: 0 }}
          onClick={(state) => {
            const minute = Number(state?.activeLabel);
            if (Number.isFinite(minute)) onScrub(minute);
          }}
        >
          <XAxis
            type="number"
            dataKey="minute"
            domain={[0, last]}
            ticks={ticks}
            tickFormatter={(m: number) => clockLabel(view.episode.start, m)}
            tick={text}
            axisLine={{ stroke: "var(--axis)" }}
            tickLine={false}
            allowDataOverflow
          />
          <YAxis
            domain={[0, axis.max]}
            ticks={axis.ticks}
            width={PLOT_LEFT * scale}
            allowDecimals={false}
            tick={text}
            axisLine={false}
            tickLine={false}
          />
          <CartesianGrid vertical={false} stroke="var(--grid)" />

          {/* the model's forecast: a fan from "now" into the future */}
          <Area dataKey="forecastRange" type="monotone" stroke="none" fill="var(--predictive)" fillOpacity={0.2} isAnimationActive={false} connectNulls={false} />
          <Line dataKey="forecastMid" type="monotone" stroke="var(--predictive)" strokeWidth={2} strokeDasharray="2 4" strokeLinecap="round" dot={false} isAnimationActive={false} connectNulls={false} />

          {/* capacity that was asked for but is not ready yet */}
          <Area dataKey="reactiveBooting" type="stepAfter" stroke="none" fill="var(--reactive)" fillOpacity={0.22} isAnimationActive={false} connectNulls={false} />
          <Area dataKey="predictiveBooting" type="stepAfter" stroke="none" fill="var(--predictive)" fillOpacity={0.22} isAnimationActive={false} connectNulls={false} />

          {/* where capacity fell short of the need (EC2) */}
          <Area dataKey="reactiveGap" type="stepAfter" stroke="none" fill="var(--reactive)" fillOpacity={0.45} isAnimationActive={false} connectNulls={false} />
          <Area dataKey="predictiveGap" type="stepAfter" stroke="none" fill="var(--predictive)" fillOpacity={0.45} isAnimationActive={false} connectNulls={false} />

          <Line dataKey="reactive" type="stepAfter" stroke="var(--reactive)" strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
          <Line dataKey="predictive" type="stepAfter" stroke="var(--predictive)" strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
          <Line dataKey="need" type={view.faultKind === "over_plan" ? "monotone" : "stepAfter"} stroke="var(--actual)" strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />

          {/* the moments each system asked for more */}
          <Line dataKey="reactiveAsk" stroke="none" dot={<AskDot fill="var(--reactive)" scale={scale} />} activeDot={false} isAnimationActive={false} connectNulls={false} legendType="none" />
          <Line dataKey="predictiveAsk" stroke="none" dot={<AskDot fill="var(--predictive)" scale={scale} />} activeDot={false} isAnimationActive={false} connectNulls={false} legendType="none" />

          <ReferenceLine x={now} stroke="var(--ink-2)" strokeWidth={1} label={{ value: "now", position: "insideTopRight", fill: "var(--ink-2)", fontSize: 13 * scale, fontFamily: "var(--mono)" }} />
          <Tooltip
            cursor={{ stroke: "var(--ink-3)", strokeWidth: 1 }}
            isAnimationActive={false}
            content={(props) => <ArenaTooltip active={props.active} payload={props.payload as unknown as { payload: ChartRow }[]} view={view} />}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export const ArenaChart = memo(ArenaChartImpl);
