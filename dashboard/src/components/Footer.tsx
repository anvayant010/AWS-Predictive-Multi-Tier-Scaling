import type { DashboardMeta } from "../data/types";

export function Footer({ meta }: { meta: DashboardMeta }) {
  return (
    <footer className="footer">
      <div className="wrap">
        Replay on synthetic traffic · seed {meta.seed} · generated {meta.generated_at.slice(0, 10)} · costs are capacity-hours × list prices
        {meta.prices_verified ? "" : ", unverified"}
        <br />
        <details>
          <summary>How to read this</summary>
          Reactive scaling is approximated from AWS target tracking. Cold starts are counted on a one-minute grid. EC2 latency is not modelled. The
          AWS delays and the cold-start figures were measured on real AWS; everything else is a replay.
        </details>
      </div>
    </footer>
  );
}
