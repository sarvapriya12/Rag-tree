"use client";

import { KeyboardEvent, PointerEvent, useCallback, useEffect, useRef, useState } from "react";

/**
 * A draggable pane width, remembered in localStorage. `direction` is 1 when the pane grows as the
 * pointer moves right (a left pane) and -1 when it grows as the pointer moves left (a right pane).
 */
export function useResizable(key: string, initial: number, min: number, max: number, direction: 1 | -1 = 1) {
  const [width, setWidth] = useState(initial);
  const drag = useRef<{ x: number; width: number } | null>(null);
  const clamp = useCallback((value: number) => Math.min(max, Math.max(min, value)), [min, max]);

  useEffect(() => {
    try {
      const saved = Number(localStorage.getItem(key));
      if (saved >= min && saved <= max) setWidth(saved);
    } catch { /* storage can be unavailable; the default width is fine */ }
  }, [key, min, max]);

  useEffect(() => {
    function move(event: globalThis.PointerEvent) {
      if (drag.current) setWidth(clamp(drag.current.width + direction * (event.clientX - drag.current.x)));
    }
    function stop() { drag.current = null; }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
  }, [clamp, direction]);

  const remember = useCallback((value: number) => {
    try { localStorage.setItem(key, String(value)); } catch { /* ignore */ }
  }, [key]);

  const onPointerDown = (event: PointerEvent<HTMLElement>) => {
    drag.current = { x: event.clientX, width };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onPointerUp = () => { if (drag.current) remember(width); drag.current = null; };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const step = event.key === "ArrowLeft" ? -16 : event.key === "ArrowRight" ? 16 : 0;
    if (!step) return;
    event.preventDefault();
    const next = clamp(width + direction * step);
    setWidth(next);
    remember(next);
  };
  return { width, handle: { onPointerDown, onPointerUp, onKeyDown, role: "separator" as const,
    "aria-orientation": "vertical" as const, "aria-valuenow": width, "aria-valuemin": min, "aria-valuemax": max, tabIndex: 0 } };
}
