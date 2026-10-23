import { useState } from "react";
import type { Accuracy, Comparison as ComparisonData, ComparisonRun, Spread } from "../data/types";
import { accuracyFigures } from "../lib/accuracy";
import {
  catalogTakeaway,
  lambdaTakeaway,
  metricMax,
  position,
  rampNote,
  spreadOf,
  tickLabel,
  ticksFor,
  TIERS,
  type Marker,
  type MetricDef,
  type Takeaway,
  type TierDef,
} from "../lib/ledger";

type Ramp = "0.7" | "0.0";

const range = (spread: Spread, format: (v: number) => string): string => `${format(spread.min)} to ${format(spread.max)}`;

function Mark({ kind }: { kind: Marker }) {
  return <i className={`mk ${kind}`} aria-hidden="true" />;
}

function MetricBlock({ run, tier, metric }: { run: ComparisonRun; tier: TierDef; metric: MetricDef }) {
  const max = metricMax(run, tier, metric);
  const ticks = ticksFor(max);
  return (
    <div className="metric">
      <div className="metric-head">
        <h4>{metric.title}</h4>
        <span>{metric.hint}</span>
      </div>
      {tier.arms.map((arm) => {
        const spread = spreadOf(run, tier.key, metric.key, arm.key);
        if (!spread) return null;
        const spoken = `${arm.label}: ${metric.format(spread.median)}, lowest to highest across ${run.seeds} seeds ${range(spread, metric.format)}`;
        return (
          <div className={`lrow ${arm.marker}`} key={arm.key} title={spoken}>
            <div className="lname">
              <Mark kind={arm.marker} />
              <span>
                {arm.label}
                <small>{arm.note}</small>
              </span>
            </div>
            <div className="ltrack" role="img" aria-label={spoken}>
              {ticks.map((tick) => (
                <i className="lgrid" key={tick} style={{ left: `${position(tick, max)}%` }} />
              ))}
              <span className={`lwhisker${spread.max - spread.min < 1e-9 ? " flat" : ""}`} style={{ left: `${position(spread.min, max)}%`, width: `${position(spread.max, max) - position(spread.min, max)}%` }} />
              <span className="ldot" style={{ left: `${position(spread.median, max)}%` }}>
                <Mark kind={arm.marker} />
              </span>
            </div>
            <div className="lvalue mono">
              <b>{metric.format(spread.median)}</b>
              <small>{metric.format(spread.min) === metric.format(spread.max) ? "same in every seed" : `${metric.format(spread.min)}–${metric.format(spread.max)}`}</small>
            </div>
          </div>
        );
      })}
      <div className="lrow axis" aria-hidden="true">
        <div />
        <div className="ltrack ticks">
          {ticks.map((tick, i) => (
            <span key={tick} className={i === 0 ? "first" : i === ticks.length - 1 ? "last" : ""} style={{ left: `${position(tick, max)}%` }}>
              {tickLabel(tick)}
            </span>
          ))}
        </div>
        <div />
      </div>
    </div>
  );
}

function LedgerTable({ run, tier }: { run: ComparisonRun; tier: TierDef }) {
  return (
    <details className="tableview">
      <summary>Show the numbers as a table</summary>
      <div className="tablewrap" tabIndex={0}>
        <table>
          <caption className="sr-only">
            {tier.title}: median, with the lowest and highest of {run.seeds} seeds
          </caption>
          <thead>
            <tr>
              <th scope="col">approach</th>
              {tier.metrics.map((metric) => (
                <th scope="col" key={metric.key}>
                  {metric.title.toLowerCase()}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tier.arms.map((arm) => (
              <tr key={arm.key}>
                <th scope="row">{arm.label}</th>
                {tier.metrics.map((metric) => {
                  const spread = spreadOf(run, tier.key, metric.key, arm.key);
                  return <td key={metric.key}>{spread ? `${metric.format(spread.median)} (${metric.format(spread.min)}–${metric.format(spread.max)})` : "–"}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

interface LedgerProps {
  tier: TierDef;
  run: ComparisonRun;
  takeaway: Takeaway | null;
  extra?: React.ReactNode;
  toolbar?: React.ReactNode;
}

function Ledger({ tier, run, takeaway, extra, toolbar }: LedgerProps) {
  return (
    <article className="ledger" aria-labelledby={`ledger-${tier.key}`}>
      <header className="ledger-head">
        <h3 id={`ledger-${tier.key}`}>
          {tier.title} <span className="tierchip mono">{tier.tier}</span>
        </h3>
        {toolbar}
      </header>
      {takeaway && (
        <p className="takeaway">
          <b>{takeaway.lead}</b> {takeaway.detail}
        </p>
      )}
      {extra}
      <p className="lkey" aria-hidden="true">
        <span>
          <i className="sample dot" /> median of {run.seeds} seeded replays
        </span>
        <span>
          <i className="sample bar" /> lowest to highest seed
        </span>
      </p>
      {tier.metrics.map((metric) => (
        <MetricBlock key={metric.key} run={run} tier={tier} metric={metric} />
      ))}
      <LedgerTable run={run} tier={tier} />
    </article>
  );
}

function RampToggle({ value, onChange }: { value: Ramp; onChange: (value: Ramp) => void }) {
  return (
    <div className="ramp">
      <span id="ramp-label">Bursts that warn of themselves</span>
      <div className="seg" role="group" aria-labelledby="ramp-label">
        {(["0.7", "0.0"] as const).map((option) => (
          <button key={option} type="button" aria-pressed={value === option} onClick={() => onChange(option)}>
            {option === "0.7" ? "70%" : "0%"}
          </button>
        ))}
      </div>
    </div>
  );
}

interface Props {
  comparison: ComparisonData | undefined;
  accuracy: Record<string, Accuracy>;
  provisionedMinutes: number;
}

function AccuracyStrip({ accuracy }: { accuracy: Record<string, Accuracy> }) {
  const figures = accuracyFigures(accuracy);
  if (figures.length === 0) return null;
  return (
    <section className="acc" aria-labelledby="acc-title">
      <h3 className="acc-title" id="acc-title">
        How good is the forecast itself?
      </h3>
      <ul className="acc-grid">
        {figures.map((figure) => (
          <li key={figure.key}>
            <b className="acc-v mono">{figure.value}</b>
            <span className="acc-l">{figure.label}</span>
            <small>{figure.detail}</small>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ComparisonLedger({ comparison, accuracy, provisionedMinutes }: Props) {
  const [ramp, setRamp] = useState<Ramp>("0.7");
  const base = comparison?.["0.7"];
  if (!comparison || !base) {
    return (
      <div className="placeholder">
        <h3>No comparison file was found</h3>
        Run python -m comparison.replay_compare --seeds 42 1 2 3 4, then python -m scripts.export_dashboard_data.
      </div>
    );
  }
  const lambdaRun = comparison[ramp] ?? base;
  const note = rampNote(comparison);
  const lambdaTakeaways = lambdaTakeaway(lambdaRun);
  const tie = lambdaTakeaways?.lead.startsWith("No clear") ?? false;
  const catalog = TIERS[0];
  const lambda = TIERS[1];
  if (!catalog || !lambda) return null;

  return (
    <div className="ledgers">
      <Ledger tier={catalog} run={base} takeaway={catalogTakeaway(base)} />
      <Ledger
        tier={lambda}
        run={lambdaRun}
        takeaway={lambdaTakeaways}
        toolbar={comparison["0.0"] ? <RampToggle value={ramp} onChange={setRamp} /> : undefined}
        extra={
          <p className="why">
            {tie ? "This is what the delay predicts, not a fault in the forecast: " : "What limits it: "}a provisioned container takes about {provisionedMinutes} minutes to become ready and a
            burst lasts only a few, so by the time the help arrives much of the burst has passed. {ramp === "0.0" ? "No burst gives any advance sign in this run. " : ""}
            {note}
          </p>
        }
      />
      <AccuracyStrip accuracy={accuracy} />
      <ul className="care" aria-label="Read these results with care">
        <li>
          <b>Predictive had a head start.</b> Its forecaster was trained on the same generator that produced this traffic. A fresh deployment would not have that.
        </li>
        <li>
          <b>The reactive rules are approximations</b> of AWS target tracking. "Always at peak" is a reference that knows the week's peak in advance; nobody can deploy it.
        </li>
        <li>
          <b>Not tested:</b> a plain timetable (more instances in the morning, fewer at night). For the smooth daily catalog cycle it may match the forecaster.
        </li>
      </ul>
    </div>
  );
}
