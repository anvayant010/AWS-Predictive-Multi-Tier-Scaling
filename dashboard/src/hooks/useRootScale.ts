import { useEffect, useState } from "react";

/** How many times larger than the 16px base the page's root text size currently is (it grows on wide screens). */
function readScale(): number {
  if (typeof window === "undefined") return 1;
  const size = parseFloat(window.getComputedStyle(document.documentElement).fontSize);
  return Number.isFinite(size) && size > 0 ? size / 16 : 1;
}

/** Lets pixel-based drawing (the chart) grow with the rem-based page. */
export function useRootScale(): number {
  const [scale, setScale] = useState(readScale);
  useEffect(() => {
    const update = () => setScale(readScale());
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  return scale;
}
