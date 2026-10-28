import { useEffect, useRef, useState } from 'react';

const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

/** Counts from the previous value to the new one. */
export function useCountUp(target: number | null, duration = 900): number | null {
  const [value, setValue] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    if (target === null || from.current === null || reducedMotion()) { from.current = target; setValue(target); return; }
    const start = from.current, began = performance.now();
    if (start === target) return;
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - began) / duration), eased = 1 - Math.pow(1 - t, 3);
      setValue(Math.round(start + (target - start) * eased));
      if (t < 1) frame = requestAnimationFrame(step);
      else from.current = target;
    };
    frame = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(frame); from.current = target; };
  }, [target, duration]);
  return value;
}
