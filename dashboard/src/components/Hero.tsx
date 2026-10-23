import type { AwsFacts, Comparison, DashboardMeta } from "../data/types";
import { formatInt, formatPct, formatUsd } from "../lib/format";
import { Term } from "./Term";

interface Props {
  comparison: Comparison | undefined;
  aws: AwsFacts;
  meta: DashboardMeta;
}

/** The hero figure is the catalog result only; the wording says so. Lambda is reported in its own section. */
export function Hero({ comparison, aws, meta }: Props) {
  const planRpm = meta.declared_rpm_per_instance * meta.plan_utilization;
  const startSeconds = Math.round(aws.instance_healthy_seconds / 10) * 10;
  const coldSeconds = (aws.cold_start_penalty_ms / 1000).toFixed(1);
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
        Cloud apps usually scale reactively: capacity is added after the traffic has already arrived, and new capacity takes time to start. This system
        forecasts the next 15 minutes of demand and scales up before it is needed.
      </p>
      <div className="hero-more">
        <p>
          <b>Why it matters.</b> A new server takes about {startSeconds} seconds to start. A serverless function that has to start a fresh container makes that
          one request wait (a{" "}
          <Term def={`The first request to a brand-new container waits while it starts up. Measured on AWS for this project: about ${coldSeconds} seconds longer than a warm one.`}>
            cold start
          </Term>
          ). A reactive system is always a few minutes behind a surge; one that sees it coming can start early.
        </p>
        <p>
          <b>How it works.</b> A machine-learning model reads recent traffic and forecasts the next 15 minutes, as a likely value and a range. A decision engine,
          with hard safety limits, turns that into a request to AWS: more instances for a web service (EC2), and more containers kept ready ahead of time for a
          function (Lambda{" "}
          <Term def="Containers you pay to keep ready ahead of time, so requests never wait for a start-up. Setting them up takes about 3 minutes.">provisioned concurrency</Term>
          ).
        </p>
        <p>
          <b>What you are looking at.</b> A recorded week of traffic, replayed against a reactive baseline, using start-up delays measured on a real AWS deployment.
          The traffic is synthetic and nothing here runs live. Press play below, then see the whole-week comparison and the measurements.
        </p>
        <p>
          <b>What it found.</b> For the web service, much more time inside its capacity plan than standard reactive scaling, at a similar cost. For the function,
          no clear difference in cold starts.
        </p>
      </div>
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
              <p className="sub" style={{ margin: "0.25rem 0 0" }}>
                Median of {comparison?.["0.7"]?.seeds ?? "several"} seeded replays. Protection like the aggressive setting, at close to standard cost.
              </p>
            </div>
          )}
          <p className="hero-explain">
            <b>What “inside the plan” means.</b> Each instance is planned to carry up to {formatInt(planRpm)} requests a minute: {Math.round(meta.plan_utilization * 100)}% of the{" "}
            {formatInt(meta.declared_rpm_per_instance)} it is declared to handle, which leaves headroom for surprises. A reactive system adds capacity only after it sees
            load pass that line, so it spends more of the week above it.
          </p>
        </div>
      )}
    </div>
  );
}
