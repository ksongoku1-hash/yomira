import { useEffect, type RefObject } from "react";

export function useInterfaceMotion(root: RefObject<HTMLElement | null>, path: string, enabled: boolean) {
  useEffect(() => {
    const container = root.current;
    if (!enabled || !container || typeof container.animate !== "function") return;
    const content = container.querySelector("main") ?? container;
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const pointer = window.matchMedia?.("(hover: hover) and (pointer: fine)");
    const animations = new Map<Animation, HTMLElement>();
    let press: HTMLElement | null = null;
    let pressAnimation: Animation | undefined;
    let surface: HTMLElement | null = null;
    let frame = 0;
    let x = 0;
    let y = 0;
    const clearPress = () => {
      pressAnimation?.cancel(); pressAnimation = undefined;
      press?.remove(); press = null;
    };
    const clearSurface = () => {
      cancelAnimationFrame(frame); frame = 0;
      if (!surface) return;
      surface.removeAttribute("data-motion-active");
      surface.removeAttribute("data-motion-static");
      for (const name of ["--surface-x", "--surface-y", "--surface-rx", "--surface-ry"]) surface.style.removeProperty(name);
      surface = null;
    };
    const move = (event: PointerEvent) => {
      if (event.pointerType !== "mouse" || document.hidden) return;
      const target = event.target instanceof Element ? event.target : null;
      const next = target?.closest<HTMLElement>("[data-motion-surface='cover'], button:not(:disabled), a[href], [data-motion-surface='hero']") ?? null;
      if (!next || !container.contains(next) || next.closest("[hidden], [inert]")) { clearSurface(); return; }
      if (next !== surface) {
        clearSurface(); surface = next;
        if (getComputedStyle(next).position === "static") next.setAttribute("data-motion-static", "true");
      }
      x = event.clientX; y = event.clientY;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (!surface?.isConnected) { clearSurface(); return; }
        const bounds = surface.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return;
        const px = Math.max(0, Math.min(1, (x - bounds.left) / bounds.width));
        const py = Math.max(0, Math.min(1, (y - bounds.top) / bounds.height));
        surface.style.setProperty("--surface-x", `${px * 100}%`);
        surface.style.setProperty("--surface-y", `${py * 100}%`);
        surface.style.setProperty("--surface-rx", `${(0.5 - py) * 10}deg`);
        surface.style.setProperty("--surface-ry", `${(px - 0.5) * 12}deg`);
        surface.setAttribute("data-motion-active", "true");
      });
    };
    const leave = (event: PointerEvent) => {
      if (!surface || !(event.relatedTarget instanceof Node) || !surface.contains(event.relatedTarget)) clearSurface();
    };
    const clearInteraction = () => { clearSurface(); clearPress(); };
    const visibility = () => { if (document.hidden) clearInteraction(); };
    const focus = (event: FocusEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      animations.forEach((element, animation) => {
        if (element.contains(target)) { animation.cancel(); animations.delete(animation); }
      });
    };
    const activate = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || document.hidden) return;
      const control = event.target instanceof Element ? event.target.closest<HTMLElement>("button, a[href]") : null;
      if (!control || !container.contains(control) || control.matches(":disabled, [aria-disabled='true']") || control.closest("[hidden], [inert]")) return;
      const dialog = control.closest("dialog[open]");
      // A transformed dialog changes the containing block of fixed feedback.
      if (dialog && getComputedStyle(dialog).transform !== "none") return;
      const bounds = control.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      clearPress();
      press = document.createElement("span");
      press.setAttribute("data-motion-press", ""); press.setAttribute("aria-hidden", "true");
      Object.assign(press.style, { left: `${bounds.left}px`, top: `${bounds.top}px`, width: `${bounds.width}px`, height: `${bounds.height}px`, borderRadius: getComputedStyle(control).borderRadius });
      const wave = document.createElement("span");
      const size = Math.min(480, Math.hypot(bounds.width, bounds.height) * 2);
      Object.assign(wave.style, { width: `${size}px`, height: `${size}px`,
        left: `${event.detail === 0 ? bounds.width / 2 : Math.max(0, Math.min(bounds.width, event.clientX - bounds.left))}px`,
        top: `${event.detail === 0 ? bounds.height / 2 : Math.max(0, Math.min(bounds.height, event.clientY - bounds.top))}px` });
      press.append(wave); (dialog ?? container).append(press);
      const currentPress = press;
      const animation = wave.animate([
        { opacity: .65, transform: "translate(-50%, -50%) scale(.1)" },
        { opacity: 0, transform: "translate(-50%, -50%) scale(1)" },
      ], { duration: 420, easing: "cubic-bezier(.2,.7,.2,1)" });
      pressAnimation = animation;
      const finish = () => { if (press === currentPress) clearPress(); };
      void animation.finished.then(finish, finish);
    };
    let generation = 0;
    let intersection: IntersectionObserver | undefined;
    let mutations: MutationObserver | undefined;
    const stop = () => {
      generation++;
      intersection?.disconnect(); mutations?.disconnect();
      animations.forEach((_element, animation) => animation.cancel()); animations.clear();
      container.removeEventListener("pointermove", move);
      container.removeEventListener("pointerout", leave);
      container.removeEventListener("click", activate);
      container.removeEventListener("focusin", focus);
      window.removeEventListener("blur", clearInteraction);
      window.removeEventListener("scroll", clearInteraction, true);
      document.removeEventListener("visibilitychange", visibility);
      clearInteraction();
    };
    const start = () => {
      stop();
      if (media?.matches) return;
      container.addEventListener("click", activate, { passive: true });
      container.addEventListener("focusin", focus);
      window.addEventListener("blur", clearInteraction);
      window.addEventListener("scroll", clearInteraction, { capture: true, passive: true });
      document.addEventListener("visibilitychange", visibility);
      if (pointer?.matches) {
        container.addEventListener("pointermove", move, { passive: true });
        container.addEventListener("pointerout", leave, { passive: true });
      }
      const current = generation;
      const seen = new WeakSet<Element>();
      const play = (element: HTMLElement, card = false) => {
        if (current !== generation || media?.matches || !element.isConnected || seen.has(element) || element.contains(document.activeElement)) return;
        seen.add(element);
        const index = card && element.parentElement ? [...element.parentElement.children].indexOf(element) : 0;
        const animation = element.animate([
          { opacity: 0, transform: card ? "perspective(900px) translateY(24px) rotateX(6deg) scale(.97)" : "translateY(22px)" },
          { opacity: 1, transform: "none" },
        ], { duration: card ? 650 : 700, delay: Math.min(index, 4) * 70,
          easing: "cubic-bezier(.2,.7,.2,1)", fill: "backwards" });
        animations.set(animation, element);
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
        if (current !== generation || element.hasAttribute("data-motion-press")) return;
        if (element === content.lastElementChild && !element.hasAttribute("role")) play(element as HTMLElement);
        if (element.matches("[data-motion-reveal], [data-motion-card]")) intersection?.observe(element);
        element.querySelectorAll("[data-motion-reveal], [data-motion-card]").forEach((child) => intersection?.observe(child));
      };
      [...content.children].forEach(track);
      mutations = new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach((node) => {
        if (node instanceof HTMLElement) track(node);
      })));
      mutations.observe(content, { childList: true, subtree: true });
    };
    start(); media?.addEventListener?.("change", start);
    pointer?.addEventListener?.("change", start);
    return () => { stop(); media?.removeEventListener?.("change", start); pointer?.removeEventListener?.("change", start); };
  }, [root, path, enabled]);
}
