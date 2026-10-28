import { useEffect, type RefObject } from "react";

export function useInterfaceMotion(root: RefObject<HTMLElement | null>, path: string, enabled: boolean) {
  useEffect(() => {
    const container = root.current;
    if (!enabled || !container || typeof container.animate !== "function") return;
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const animations = new Set<Animation>();
    let generation = 0;
    let intersection: IntersectionObserver | undefined;
    let mutations: MutationObserver | undefined;
    const stop = () => {
      generation++;
      intersection?.disconnect(); mutations?.disconnect();
      animations.forEach((animation) => animation.cancel()); animations.clear();
    };
    const start = () => {
      stop();
      if (media?.matches) return;
      const current = generation;
      const seen = new WeakSet<Element>();
      const play = (element: HTMLElement, card = false) => {
        if (current !== generation || media?.matches || !element.isConnected || seen.has(element) || element.contains(document.activeElement)) return;
        seen.add(element);
        const index = card && element.parentElement ? [...element.parentElement.children].indexOf(element) : 0;
        const animation = element.animate([
          { opacity: 0, transform: "translateY(22px)" }, { opacity: 1, transform: "none" },
        ], { duration: card ? 650 : 700, delay: Math.min(index, 4) * 70,
          easing: "cubic-bezier(.2,.7,.2,1)", fill: "backwards" });
        animations.add(animation);
        void animation.finished.then(() => animations.delete(animation), () => animations.delete(animation));
      };
      if (typeof IntersectionObserver === "function") {
        intersection = new IntersectionObserver((entries) => entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          intersection?.unobserve(entry.target);
          play(entry.target as HTMLElement, entry.target.hasAttribute("data-motion-card"));
        }), { threshold: .1 });
      }
      const track = (element: Element) => {
        if (current !== generation) return;
        if (element === container.lastElementChild && !element.hasAttribute("role")) play(element as HTMLElement);
        if (element.matches("[data-motion-reveal], [data-motion-card]")) intersection?.observe(element);
        element.querySelectorAll("[data-motion-reveal], [data-motion-card]").forEach((child) => intersection?.observe(child));
      };
      [...container.children].forEach(track);
      mutations = new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach((node) => {
        if (node instanceof HTMLElement) track(node);
      })));
      mutations.observe(container, { childList: true, subtree: true });
    };
    start(); media?.addEventListener?.("change", start);
    return () => { stop(); media?.removeEventListener?.("change", start); };
  }, [root, path, enabled]);
}
