import { useEffect, useRef, useState } from "react";

export function ProgressPercentage({ value, animate }: { value: number; animate: boolean }) {
  const element = useRef<HTMLSpanElement>(null);
  const [frame, setFrame] = useState<{ value: number; shown: number } | null>(null);
  useEffect(() => {
    if (!animate || !value || !element.current || typeof IntersectionObserver !== "function") return;
    let request = 0;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      const started = performance.now();
      const step = (now: number) => {
        const fraction = Math.max(0, Math.min(1, (now - started) / 1200));
        setFrame({ value, shown: Math.round(value * (1 - (1 - fraction) ** 3)) });
        if (fraction < 1) request = requestAnimationFrame(step);
      };
      request = requestAnimationFrame(step);
    }, { threshold: .1 });
    observer.observe(element.current);
    return () => { observer.disconnect(); cancelAnimationFrame(request); };
  }, [value, animate]);
  return <span ref={element}>{animate && frame?.value === value ? frame.shown : value}</span>;
}
