import type { Player } from "../hooks/usePlayer";
import { SPEEDS } from "../hooks/usePlayer";
import { clockLabel } from "../lib/format";

interface Props {
  player: Player;
  start: string;
  minutes: number;
  /** Replay minutes worth marking on the scrub bar (e.g. when a burst starts and ends). */
  marks?: { minute: number; label: string }[];
}

/** Play/pause, a scrub bar and a speed choice, in the manner of a media player. */
export function PlayerDock({ player, start, minutes, marks = [] }: Props) {
  const last = Math.max(1, minutes - 1);
  const label = player.playing ? "Pause" : player.atEnd ? "Replay" : "Play";
  return (
    <div className="dock">
      <button type="button" className="dock-play" onClick={player.toggle} aria-label={label}>
        {player.playing ? (
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <rect x="3" y="2" width="3.4" height="12" rx="1" fill="currentColor" />
            <rect x="9.6" y="2" width="3.4" height="12" rx="1" fill="currentColor" />
          </svg>
        ) : player.atEnd ? (
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M3 8a5 5 0 1 0 1.6-3.7M3 2.5v3h3" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : (
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M4 2.4v11.2a.6.6 0 0 0 .9.5l9-5.6a.6.6 0 0 0 0-1L4.9 1.9a.6.6 0 0 0-.9.5z" fill="currentColor" />
          </svg>
        )}
      </button>
      <div className="dock-clock mono" aria-live="off">
        {clockLabel(start, player.now)}
      </div>
      <div className="dock-scrub">
        <input
          type="range"
          min={0}
          max={last}
          step={1}
          value={player.now}
          onChange={(event) => {
            player.pause();
            player.seek(Number(event.target.value));
          }}
          aria-label="Position in the replay"
          aria-valuetext={`${clockLabel(start, player.now)}, minute ${player.now + 1} of ${minutes}`}
          style={{ ["--fill" as string]: `${(player.now / last) * 100}%` }}
        />
        {marks.map((mark) => (
          <span key={mark.label} className="dock-mark" style={{ left: `${(mark.minute / last) * 100}%` }} title={mark.label}>
            <i />
          </span>
        ))}
      </div>
      <div className="dock-speed" role="group" aria-label="Replay speed">
        {SPEEDS.map((speed) => (
          <button key={speed} type="button" aria-pressed={player.speed === speed} onClick={() => player.setSpeed(speed)}>
            {speed}×
          </button>
        ))}
      </div>
    </div>
  );
}
