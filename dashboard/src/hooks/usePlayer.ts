import { useCallback, useEffect, useRef, useState } from "react";

export const SPEEDS = [1, 3, 6] as const; // replay minutes per real second
export type Speed = (typeof SPEEDS)[number];

export interface Player {
  now: number;
  playing: boolean;
  speed: Speed;
  atEnd: boolean;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  restart: () => void;
  seek: (minute: number) => void;
  setSpeed: (speed: Speed) => void;
}

/**
 * Plays a recorded episode forward one replay minute at a time. `now` only changes on whole minutes,
 * so the page re-renders a few times a second rather than every animation frame.
 */
export function usePlayer(minutes: number, defaultSpeed: Speed = 3, startAt = 0): Player {
  const last = Math.max(0, minutes - 1);
  const initial = Math.max(0, Math.min(last, Math.round(startAt)));
  const [now, setNow] = useState(initial);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(defaultSpeed);
  const position = useRef(initial); // fractional minute, so slow speeds still advance smoothly
  const speedRef = useRef(speed);
  speedRef.current = speed;

  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    let previous = performance.now();
    const tick = (time: number) => {
      const seconds = Math.min((time - previous) / 1000, 0.25); // a backgrounded tab must not fast-forward the replay
      previous = time;
      position.current = Math.min(last, position.current + seconds * speedRef.current);
      setNow(Math.floor(position.current));
      if (position.current >= last) {
        setPlaying(false);
        return;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, last]);

  const seek = useCallback(
    (minute: number) => {
      const clamped = Math.max(0, Math.min(last, Math.round(minute)));
      position.current = clamped;
      setNow(clamped);
    },
    [last],
  );

  const restart = useCallback(() => {
    position.current = 0;
    setNow(0);
    setPlaying(true);
  }, []);

  const toggle = useCallback(() => {
    if (position.current >= last) {
      restart();
      return;
    }
    setPlaying((p) => !p);
  }, [last, restart]);

  return {
    now,
    playing,
    speed,
    atEnd: now >= last,
    play: useCallback(() => setPlaying(position.current < last), [last]),
    pause: useCallback(() => setPlaying(false), []),
    toggle,
    restart,
    seek,
    setSpeed,
  };
}
