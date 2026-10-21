import type { ReactNode } from "react";

/** A word with a plain-language definition on hover or keyboard focus. The definition is also in the text for screen readers. */
export function Term({ def, children }: { def: string; children: ReactNode }) {
  return (
    <span className="term" tabIndex={0} data-def={def}>
      {children}
      <span className="sr-only"> ({def})</span>
    </span>
  );
}
