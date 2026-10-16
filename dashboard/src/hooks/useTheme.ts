import { useCallback, useEffect, useState } from "react";
import { otherTheme, readStoredTheme, resolveTheme, storeTheme, type Theme } from "../lib/theme";

const systemPrefersDark = (): boolean => window.matchMedia("(prefers-color-scheme: dark)").matches;

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(() => resolveTheme(readStoredTheme(), systemPrefersDark()));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = otherTheme(current);
      storeTheme(next);
      return next;
    });
  }, []);

  return { theme, toggle };
}
