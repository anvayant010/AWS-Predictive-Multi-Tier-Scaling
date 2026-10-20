import type { ArmView, EpisodeView } from "../lib/episode";
import { faultsSoFar } from "../lib/episode";
import { PLOT_LEFT, PLOT_RIGHT } from "./ArenaChart";

interface Props {
  view: EpisodeView;
  now: number;
}

function Strip({ arm, color, view, now }: { arm: ArmView; color: string; view: EpisodeView; now: number }) {
  const minutes = view.episode.minutes;
  const step = 100 / Math.max(1, minutes - 1);
  const total = faultsSoFar(arm, now);
  const cold = view.faultKind === "cold_start";
  const noun = cold ? (total === 1 ? "cold start" : "cold starts") : total === 1 ? "minute over plan" : "minutes over plan";
  const maxPerMinute = Math.max(1, ...arm.faults);
  return (
    <div className="strip">
      <div className="strip-line" style={{ paddingLeft: PLOT_LEFT }}>
        <i className="key" style={{ background: color }} aria-hidden="true" />
        <span>{arm.label}</span>
        <b>{total}</b>
        <span className="strip-noun">{noun}</span>
      </div>
      <div className="strip-track" style={{ marginLeft: PLOT_LEFT, marginRight: PLOT_RIGHT }}>
        {arm.faults.map((value, i) => {
          if (i > now || value <= 0) return null;
          const severe = (arm.saturated[i] ?? 0) > 0;
          return (
            <span
              key={i}
              className={`strip-bar ${severe ? "severe" : cold ? "cold" : "warn"}`}
              style={{ left: `calc(${i * step}% - ${step / 2}%)`, width: `${step}%`, height: `${30 + (70 * value) / maxPerMinute}%` }}
            />
          );
        })}
      </div>
    </div>
  );
}

/** One thin row per system: a bar on every minute it fell short (amber, EC2) or paid a cold start (red, Lambda). */
export function FaultStrips({ view, now }: Props) {
  const heading = view.faultKind === "cold_start" ? "Cold starts: a request that had to wait for a new container" : "Minutes over plan: traffic above what the capacity plan allows";
  return (
    <div className="strips" role="group" aria-label={heading}>
      <div className="strips-head">
        <span className="kicker" style={{ marginLeft: PLOT_LEFT }}>
          {heading}
        </span>
      </div>
      <Strip arm={view.predictive} color="var(--predictive)" view={view} now={now} />
      <Strip arm={view.reactive} color="var(--reactive)" view={view} now={now} />
    </div>
  );
}
