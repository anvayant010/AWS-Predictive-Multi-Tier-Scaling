import type { DashboardData } from "../data/types";
import { delayAxis, delayBars, delaySentence } from "../lib/awsPanel";
import { formatInt } from "../lib/format";

const first = (text: string): string => (text[0]?.toUpperCase() ?? "") + text.slice(1);

/** Section 03: what was measured on a deployed stack, set against how long bursts last, and what is real versus replayed. */
export function AwsPanel({ data }: { data: DashboardData }) {
  const { aws, meta } = data;
  const bars = delayBars(data);
  const axis = delayAxis(bars);
  const sentence = delaySentence(bars);
  const seconds = (value: number) => (Number.isInteger(value) ? String(value) : value.toFixed(1));

  const facts = [
    { value: `${seconds(aws.instance_healthy_seconds)} s`, label: "for a new instance to become healthy", note: "Median of 3 launches." },
    { value: `${seconds(aws.provisioned_ready_seconds)} s`, label: "for provisioned containers to be ready", note: "Median of 3 requests." },
    {
      value: `+${seconds(aws.cold_start_penalty_ms)} ms`,
      label: "extra wait on a cold start",
      note: `Our marker counted ${aws.cold_starts_matched_aws_logs.marker} cold starts; AWS's own logs showed ${aws.cold_starts_matched_aws_logs.aws_init_lines}.`,
    },
    {
      value: formatInt(aws.single_instance_rpm_served),
      label: "requests a minute from one instance",
      note: `No limit found: 95th-percentile latency stayed near ${aws.single_instance_p95_ms} ms, CPU about ${aws.single_instance_cpu_pct}%.`,
    },
  ];

  const real = [
    ...aws.also_real.map(first),
    `Cold starts counted by our marker matched AWS's logs (${aws.cold_starts_matched_aws_logs.marker} against ${aws.cold_starts_matched_aws_logs.aws_init_lines}).`,
  ];
  const replayed = [
    "All traffic: generated, not recorded from real users.",
    "The week of forecasts and scaling decisions.",
    "Costs: capacity-hours times list prices that have not been checked.",
    `Delays are rounded up to whole minutes in the replay (${meta.delays_minutes.instance} and ${meta.delays_minutes.provisioned_concurrency}).`,
  ];

  return (
    <div className="measured">
      <p className="measured-head mono">
        <i className="awsmark" aria-hidden="true" />
        Measured on a deployed stack · {aws.region} · {aws.measured_on}
      </p>

      <ul className="facts">
        {facts.map((fact) => (
          <li key={fact.label}>
            <b className="mono">{fact.value}</b>
            <span>{fact.label}</span>
            <small>{fact.note}</small>
          </li>
        ))}
      </ul>

      <div className="delays" role="group" aria-label="How long things take on AWS, against how long recorded bursts last">
        <h3>The clock, against the bursts</h3>
        {bars.map((bar) => (
          <div className={`delay-row ${bar.kind}`} key={bar.key}>
            <span className="delay-label">{bar.label}</span>
            <div className="delay-track" role="img" aria-label={`${bar.label}: ${bar.value}`}>
              {axis.ticks.map((tick) => (
                <i className="delay-grid" key={tick} style={{ left: `${(tick / axis.max) * 100}%` }} />
              ))}
              <span className="delay-bar" style={{ width: `${(bar.minutes / axis.max) * 100}%` }} />
            </div>
            <span className="delay-value mono">{bar.value}</span>
          </div>
        ))}
        <div className="delay-row axis" aria-hidden="true">
          <span />
          <div className="delay-track ticks">
            {axis.ticks.map((tick) => (
              <span key={tick} style={{ left: `${(tick / axis.max) * 100}%` }}>
                {tick}
              </span>
            ))}
          </div>
          <span className="delay-value mono">minutes</span>
        </div>
        <p className="delay-key">
          <span>
            <i className="swatch aws" /> measured on AWS
          </span>
          <span>
            <i className="swatch burst" /> how long a burst lasted in the replay
          </span>
        </p>
        {sentence && <p className="delay-sentence">{sentence}</p>}
      </div>

      <div className="split">
        <section aria-labelledby="real-h">
          <h3 id="real-h">
            <i className="awsmark" aria-hidden="true" /> Real, on AWS
          </h3>
          <ul>
            {real.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
        <section aria-labelledby="replay-h">
          <h3 id="replay-h">Replayed, not run live</h3>
          <ul>
            {replayed.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      </div>

      <p className="measured-note">Measured once, on one small instance running a trivial handler. Real services, and real traffic, will behave differently.</p>
    </div>
  );
}
