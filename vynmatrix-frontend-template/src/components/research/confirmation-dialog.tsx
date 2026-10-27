"use client";

import { useEffect, useRef, type KeyboardEvent } from "react";

export function ConfirmationDialog({ description, lines, consequence, busy, disabled, onCancel, onConfirm, title = "Confirm paper change", confirmLabel = "Confirm change" }: { description: string; lines: string[]; consequence: string; busy: boolean; disabled: boolean; onCancel: () => void; onConfirm: () => void; title?: string; confirmLabel?: string }) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { document.body.style.overflow = previousOverflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  function keyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape" && !busy) { event.preventDefault(); onCancel(); }
    if (event.key !== "Tab") return;
    const buttons = panel.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
    if (!buttons?.length) { event.preventDefault(); panel.current?.focus(); return; }
    const first = buttons[0]; const last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  return <div className="research-modal-backdrop"><section ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="research-confirm-title" className="research-modal" onKeyDown={keyDown}>
    <h2 id="research-confirm-title">{title}</h2><p>{description}</p><ul>{lines.map((line) => <li key={line}>{line}</li>)}</ul><p>{consequence}</p>
    <div className="research-toolbar"><button className="button secondary" disabled={busy} onClick={onCancel}>Cancel</button><button className="button primary" disabled={disabled} onClick={onConfirm}>{confirmLabel}</button></div>
  </section></div>;
}
