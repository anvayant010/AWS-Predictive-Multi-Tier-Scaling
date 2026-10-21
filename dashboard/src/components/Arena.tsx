import { useEffect, useMemo, useRef, useState } from "react";
import type { DashboardData, Episode, LambdaEpisode } from "../data/types";
import { usePlayer } from "../hooks/usePlayer";
import { buildEpisodeView, episodeTabs, nextWindowOutlook, totalFaults, type EpisodeView } from "../lib/episode";
import { clockLabel, formatInt, formatPct, withUnit } from "../lib/format";
import { outcomeLine } from "../lib/lanes";
import { ArenaChart } from "./ArenaChart";
import { FaultStrips } from "./FaultStrips";
import { HowToRead } from "./HowToRead";
import { Lanes } from "./Lanes";
import { PlayerDock } from "./PlayerDock";
import { Section } from "./Section";
import { Term } from "./Term";

/** Optional URL parameters for screenshots and links: ?ep=<episode id>&t=<minute>&still=1 (no autoplay). */
function viewParams(): { ep: string | null; t: number; still: boolean } {
  const params = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  const t = Number(params.get("t"));
  return { ep: params.get("ep"), t: Number.isFinite(t) ? t : 0, still: params.get("still") === "1" };
}

const fmt = (value: number) => (Math.abs(value - Math.round(value)) < 0.05 ? String(Math.round(value)) : value.toFixed(1));

function Legend({ view }: { view: EpisodeView }) {
  const unit = view.unit.many;
  return (
    <ul className="legend" aria-label="Chart key">
      <li>
        <i className="key" style={{ background: "var(--actual)" }} />
        <span>{unit[0]?.toUpperCase()}{unit.slice(1)} needed</span>
      </li>
      <li>
        <i className="key" style={{ background: "var(--predictive)" }} />
        <span>Predictive: ready</span>
      </li>
      <li>
        <i className="key" style={{ background: "var(--reactive)" }} />
        <span>Reactive: ready</span>
      </li>
      <li>
        <i className="key fan" />
        <span>Model's forecast, next 15 min</span>
      </li>
      <li>
        <i className="dotkey" />
        <span>Asked for more</span>
      </li>
      <li>
        <i className="key wait" />
        <span>Requested, still starting</span>
      </li>
    </ul>
  );
}

function lambdaArmOf(ep: LambdaEpisode, name: "reactive_on_demand_only") {
  return { faults: ep.arms[name].cold };
}

function ContextLine({ view, data }: { view: EpisodeView; data: DashboardData }) {
  const ep = view.episode;
  if (ep.kind === "lambda") {
    const all = ep.all_bursts;
    const pr = all.predictive_vs_reactive;
    return (
      <p className="context">
        <b>In this replay.</b> Cold starts: predictive {totalFaults(view.predictive)}, reactive {totalFaults(view.reactive)}, doing nothing{" "}
        {totalFaults(lambdaArmOf(ep, "reactive_on_demand_only"))}.{" "}
        {pr && (
          <>
            <b>Across all {all.bursts} bursts of the week</b> (from 5 minutes before each burst to 5 after): predictive had fewer cold starts than the reactive
            rule in {pr.better}, the same in {pr.same}, more in {pr.worse}. Averages: {all.mean_cold_starts.predictive?.toFixed(1)} against{" "}
            {all.mean_cold_starts.reactive_pc_tracking?.toFixed(1)}.
          </>
        )}
      </p>
    );
  }
  const c = ep.context;
  const week = data.comparison?.["0.7"];
  return (
    <p className="context">
      <b>This morning.</b> Minutes over the capacity plan: predictive {c.predictive.minutes_over_plan_here}, reactive {c.reactive_standard.minutes_over_plan_here}, a more
      aggressive reactive setting {c.reactive_aggressive.minutes_over_plan_here}.{" "}
      <b>Across this recorded week.</b> Predictive was over plan {formatPct(c.predictive.minutes_over_plan_week_pct)} of the time, reactive{" "}
      {formatPct(c.reactive_standard.minutes_over_plan_week_pct)}
      {week ? `, one seeded run; section 02 shows ${week.seeds}.` : "."}
    </p>
  );
}

function TableView({ view }: { view: EpisodeView }) {
  const ep = view.episode;
  const faultName = view.faultKind === "cold_start" ? "cold starts" : "over plan";
  return (
    <details className="tableview">
      <summary>Show the numbers as a table</summary>
      <div className="tablewrap" tabIndex={0}>
        <table>
          <caption className="sr-only">{ep.title}: one row per replay minute</caption>
          <thead>
            <tr>
              <th scope="col">time</th>
              <th scope="col">{ep.units.traffic}</th>
              <th scope="col">{view.unit.many} needed</th>
              <th scope="col">predictive ready</th>
              <th scope="col">reactive ready</th>
              <th scope="col">predictive {faultName}</th>
              <th scope="col">reactive {faultName}</th>
            </tr>
          </thead>
          <tbody>
            {ep.traffic.map((traffic, i) => (
              <tr key={i}>
                <th scope="row">{clockLabel(ep.start, i)}</th>
                <td>{formatInt(traffic)}</td>
                <td>{fmt(ep.need[i] ?? 0)}</td>
                <td>{view.predictive.capacity[i]}</td>
                <td>{view.reactive.capacity[i]}</td>
                <td>{view.predictive.faults[i]}</td>
                <td>{view.reactive.faults[i]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

interface StageProps {
  episode: Episode;
  data: DashboardData;
  onReadout: (node: React.ReactNode) => void;
}

/** One recorded episode being replayed. Remounted (via `key`) when the scenario changes, which resets the player. */
function Stage({ episode, data, onReadout }: StageProps) {
  const view = useMemo(() => buildEpisodeView(episode, data.meta.billed_seconds), [episode, data.meta.billed_seconds]);
  const params = useMemo(viewParams, []);
  const player = usePlayer(episode.minutes, episode.kind === "ec2" ? 3 : 1, params.ep === episode.id ? params.t : 0);
  const stageRef = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  // Start by itself the first time the simulation scrolls into view, unless the visitor prefers reduced motion.
  useEffect(() => {
    const node = stageRef.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    if (params.still || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!started.current && entries.some((entry) => entry.isIntersecting)) {
          started.current = true;
          player.play();
          observer.disconnect();
        }
      },
      { threshold: 0.45 },
    );
    observer.observe(node);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const delays = data.meta.delays_minutes;
  const readyDelay = episode.kind === "ec2" ? delays.instance : delays.provisioned_concurrency;
  const outlook = nextWindowOutlook(view, player.now);
  const needNow = episode.need[player.now] ?? 0;
  useEffect(() => {
    onReadout(
      <dl className="readout mono">
        <div>
          <dt>Time</dt>
          <dd>{clockLabel(episode.start, player.now)}</dd>
        </div>
        <div>
          <dt>Needed now</dt>
          <dd>
            {withUnit(fmt(needNow), view.unit)}
          </dd>
        </div>
        <div>
          <dt>Model expects within 15 min</dt>
          <dd>{outlook ? `${fmt(outlook.mid)} (${fmt(outlook.low)}–${fmt(outlook.high)})` : "–"}</dd>
        </div>
      </dl>,
    );
  }, [episode, player.now, needNow, outlook?.mid, outlook?.low, outlook?.high, view.unit, onReadout]);

  const marks =
    episode.kind === "lambda"
      ? [
          { minute: episode.burst.onset, label: "burst begins" },
          { minute: episode.burst.end, label: "burst ends" },
        ]
      : [];

  return (
    <div ref={stageRef}>
      <Legend view={view} />
      <ArenaChart view={view} now={player.now} onScrub={(minute) => { player.pause(); player.seek(minute); }} />
      <FaultStrips view={view} now={player.now} />
      <Lanes view={view} now={player.now} readyDelayMinutes={readyDelay} />
      <PlayerDock player={player} start={episode.start} minutes={episode.minutes} marks={marks} />
      <p className={`outcome${player.atEnd ? " done" : ""}`} aria-live="polite">
        {player.atEnd ? (
          <>
            <b>Result.</b> {outcomeLine(view, readyDelay)}
          </>
        ) : (
          "The result appears here when the replay reaches its end."
        )}
      </p>
      <ContextLine view={view} data={data} />
      <TableView view={view} />
    </div>
  );
}

export function Arena({ data }: { data: DashboardData }) {
  const tabs = useMemo(() => episodeTabs(data), [data]);
  const [activeId, setActiveId] = useState(() => {
    const wanted = viewParams().ep;
    return tabs.find((tab) => tab.id === wanted)?.id ?? tabs[0]?.id ?? "";
  });
  const [readout, setReadout] = useState<React.ReactNode>(null);
  const active = data.episodes.find((ep) => ep.id === activeId) ?? data.episodes[0];
  const activeTab = tabs.find((tab) => tab.id === active?.id);
  if (!active) return null;

  return (
    <Section
      id="watch"
      kicker="01 · Live simulation"
      title="Watch it decide"
      note="The same traffic hits two systems. One reads a forecast and acts early; the other reacts to what it already sees. Press play."
      aside={readout}
    >
      <div className="switch" role="group" aria-label="Choose a recorded episode">
        {tabs.map((tab) => (
          <button key={tab.id} type="button" aria-pressed={tab.id === active.id} onClick={() => setActiveId(tab.id)}>
            <span>{tab.tab}</span>
            <small>{tab.tier}</small>
          </button>
        ))}
      </div>
      {activeTab?.watching && <p className="watching">{activeTab.watching}</p>}
      <HowToRead episode={active} instanceMinutes={data.meta.delays_minutes.instance} provisionedMinutes={data.meta.delays_minutes.provisioned_concurrency} />
      <Stage key={active.id} episode={active} data={data} onReadout={setReadout} />
      <p className="caption">
        A replay of recorded model output on synthetic traffic, using the instance and container delays measured on AWS (section 03). Not a live system. The shaded fan is the model's forecast for the next 15 minutes: its middle guess as a dotted line, and the range between its 10th and 90th{" "}
        <Term def="The 10th percentile is a value the real number should fall below only about 1 time in 10; the 90th, about 9 times in 10. Between them sits the range the model expects the real value to land in about 8 times out of 10.">
          percentiles
        </Term>
        .
      </p>
    </Section>
  );
}
