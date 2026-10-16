import type { Comparison } from "../data/types";
import { formatPct, formatUsd } from "../lib/format";

interface Props {
  comparison: Comparison | undefined;
}

/** The hero figure is the catalog result only; the wording says so. Lambda is reported in its own section. */
export function Hero({ comparison }: Props) {
  const hero = comparison?.hero;
  const costs = comparison?.["0.7"]?.catalog_service;
  const rows: { label: string; arm: string; color: string; fade?: number }[] = [
    { label: "Predictive, cost per week", arm: "predictive", color: "var(--predictive)" },
    { label: "Reactive, standard", arm: "reactive_standard", color: "var(--reactive)" },
    { label: "Reactive, aggressive", arm: "reactive_aggressive", color: "var(--reactive)", fade: 0.55 },
  ];
  return (
    <div className="wrap hero" id="top">
      <div className="kicker">ML-driven scaling for EC2 and Lambda</div>
      <h1>
        Scaling <em>ahead</em> of demand, and what it actually buys.
      </h1>
      <p className="lede">
        A forecaster predicts the next minutes of traffic. A decision engine pre-scales an Auto Scaling Group and Lambda provisioned concurrency before
        the load lands. Below: it running, and an honest comparison against reactive scaling.
      </p>
      {hero && (
        <div className="hero-figs">
          <div>
            <div className="big">
              {hero.predictive_inside_plan_pct.toFixed(1)}
              <span>%</span>
            </div>
            <p className="fig-cap">
              of the week the catalog stayed inside its capacity plan. Reactive scaling managed {formatPct(hero.reactive_standard_inside_plan_pct)}.
            </p>
          </div>
          {costs && (
            <div className="vs">
              {rows.map((row) => {
                const spread = costs[row.arm]?.cost_usd;
                return (
                  <div key={row.arm}>
                    <i className="key" style={{ background: row.color, opacity: row.fade ?? 1 }} aria-hidden="true" />
                    <span>{row.label}</span>
                    <b>{spread ? formatUsd(spread.median) : "–"}</b>
                  </div>
                );
              })}
              <p className="sub" style={{ margin: "4px 0 0" }}>
                Median of {comparison?.["0.7"]?.seeds ?? "several"} seeded replays. Protection like the aggressive setting, at close to standard cost.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
