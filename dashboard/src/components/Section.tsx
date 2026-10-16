import type { ReactNode } from "react";

interface Props {
  id: string;
  kicker: string;
  title: string;
  note: ReactNode;
  /** Extra content under the note in the margin column (e.g. a live readout). */
  aside?: ReactNode;
  children: ReactNode;
}

/** A narrow margin column (kicker, heading, one sentence) beside the wide content column. */
export function Section({ id, kicker, title, note, aside, children }: Props) {
  return (
    <section className="section" id={id}>
      <div className="wrap row">
        <div className="margin">
          <div className="kicker">{kicker}</div>
          <h2>{title}</h2>
          <p>{note}</p>
          {aside}
        </div>
        <div className="content">{children}</div>
      </div>
    </section>
  );
}
