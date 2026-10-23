import { useCallback, useEffect, useState } from "react";
import { otherTheme, readStoredTheme, resolveTheme, storeTheme, type Theme } from "../lib/theme";

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(() => resolveTheme(readStoredTheme()));

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
