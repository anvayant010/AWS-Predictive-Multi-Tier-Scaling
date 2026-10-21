import type { Episode } from "../data/types";
import { formatInt } from "../lib/format";
import { Term } from "./Term";

interface Props {
  episode: Episode;
  instanceMinutes: number;
  provisionedMinutes: number;
}

/** Three short steps that say what the picture is, before anyone presses play. */
export function HowToRead({ episode, instanceMinutes, provisionedMinutes }: Props) {
  const plan = <Term def={planDef(episode)}>capacity plan</Term>;
  const two = (
    <>
      Two systems answer it. <b className="mint">Predictive</b> reads a forecast of the next 15 minutes and asks early. <b className="ember">Reactive</b> waits until the load is already there.
    </>
  );
  return (
    <section className="howto" aria-label="How to read this">
      <p className="howto-tier">
        {episode.kind === "ec2" ? (
          <>
            <b>EC2</b> instances are ordinary servers: once running they stay up, but each one takes about {instanceMinutes} minutes to start.
          </>
        ) : (
          <>
            <b>Lambda</b> has no servers to start: a container is created the moment a request needs one, and that first request waits (a{" "}
            <Term def="The first request to a brand-new container waits while it starts up. Measured on AWS for this project: about 0.4 seconds longer than a warm one.">cold start</Term>).
          </>
        )}
      </p>
      <ol className="howto-steps">
        <li>
          <span className="howto-n" aria-hidden="true">1</span>
          <span>
            The black line is how many {episode.kind === "ec2" ? "instances" : "containers"} the traffic needs, minute by minute.
          </span>
        </li>
        <li>
          <span className="howto-n" aria-hidden="true">2</span>
          <span>{two}</span>
        </li>
        <li>
          <span className="howto-n" aria-hidden="true">3</span>
          <span>
            {episode.kind === "ec2" ? (
              <>
                The squares are each system's instances: solid is serving, dashed is still starting, amber <b>!</b> means traffic is above the {plan}.
              </>
            ) : (
              <>
                The top squares are{" "}
                <Term def={`Containers you pay to keep ready ahead of time, so requests never wait for a start-up. Setting them up takes about ${provisionedMinutes} minutes.`}>
                  provisioned concurrency
                </Term>
                : ready ahead of time. The bottom squares start on demand, and a red <b>!</b> is a cold start.
              </>
            )}
          </span>
        </li>
      </ol>
    </section>
  );
}

function planDef(episode: Episode): string {
  if (episode.kind !== "ec2") return "";
  const { plan_rpm_per_instance: plan, full_rpm_per_instance: full } = episode.units;
  return `Each instance is planned to carry up to ${formatInt(plan)} requests a minute (70% of the ${formatInt(full)} it is declared to handle). Above that the plan says to add instances.`;
}
