import type { Theme } from "../lib/theme";

interface Props {
  theme: Theme;
  onToggleTheme: () => void;
}

export function TopBar({ theme, onToggleTheme }: Props) {
  return (
    <header className="topbar">
      <div className="wrap">
        <a className="brand" href="#top">
          <i aria-hidden="true" />
          Predictive Scaling
        </a>
        <div className="spacer" />
        <nav className="nav" aria-label="Sections">
          <a href="#watch">Watch it decide</a>
          <a href="#versus">Versus reactive</a>
          <a href="#measured">Measured on AWS</a>
        </nav>
        <button className="toggle" onClick={onToggleTheme} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
          {theme === "dark" ? "light" : "dark"}
        </button>
      </div>
    </header>
  );
}
