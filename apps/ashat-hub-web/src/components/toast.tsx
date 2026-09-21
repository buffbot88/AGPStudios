import { useEffect, useRef, useState } from 'react';

/**
 * Shared toast + confirm system.
 *
 * `toast()` shows a transient notification. `confirmToast()` is the
 * non-blocking replacement for window.confirm: a sticky toast with
 * Confirm/Cancel actions. `confirmModal()` is the heavy-weight variant for
 * destructive actions: a centered, focus-trapped alertdialog that must be
 * answered.
 *
 * Accessibility: confirm toasts move focus to their Cancel button on appear,
 * trap Tab within their actions, cancel on Escape, and restore focus to the
 * previously focused element on close. Transient toasts stay polite and
 * non-focusable. The <ToastHost /> renders everything; mount it once near the
 * app root.
 */

export type ToastKind = 'info' | 'success' | 'error';

type ToastAction = { label: string; value: boolean; danger?: boolean };

type ToastItem = {
  id: number;
  /** Repeats sharing a key update this toast instead of stacking a new one. */
  key?: string;
  message: string;
  kind: ToastKind;
  actions?: ToastAction[];
  /** Occurrences of a grouped failure, rendered as a ×N badge. */
  count?: number;
  /** Recent occurrences behind the Details disclosure, newest first. */
  details?: string[];
  duration: number;
  /** Details list is expanded (which also pins the toast open). */
  open?: boolean;
  /** Auto-dismiss paused because the pointer or focus is on the toast. */
  held?: boolean;
  timer?: number;
};

type ModalItem = {
  id: number;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  requireText?: string;
};

let nextId = 1;
let items: ToastItem[] = [];
let modal: ModalItem | undefined;
const resolvers = new Map<number, (value: boolean) => void>();
const modalResolvers = new Map<number, (value: boolean) => void>();
const listeners = new Set<(items: ToastItem[]) => void>();
const modalListeners = new Set<(modal: ModalItem | undefined) => void>();

function emit() {
  const snapshot = [...items];
  for (const listener of listeners) listener(snapshot);
}

function emitModal() {
  for (const listener of modalListeners) listener(modal);
}

function dismiss(id: number) {
  items = items.filter((item) => item.id !== id);
  emit();
}

function update(id: number, patch: Partial<ToastItem>): void {
  items = items.map((item) => (item.id === id ? { ...item, ...patch } : item));
  emit();
}

/** (Re)start a toast's auto-dismiss timer, clearing any previous one. */
function arm(item: ToastItem): ToastItem {
  if (item.timer) window.clearTimeout(item.timer);
  return item.duration > 0
    ? { ...item, timer: window.setTimeout(() => dismiss(item.id), item.duration) }
    : { ...item, timer: undefined };
}

function push(item: Omit<ToastItem, 'id'>): number {
  const existing = item.key ? items.find((entry) => entry.key === item.key) : undefined;
  if (existing) {
    const updated = arm({ ...existing, ...item, id: existing.id, open: existing.open, held: false });
    items = items.map((entry) => (entry.id === existing.id ? updated : entry));
    emit();
    return existing.id;
  }
  const id = nextId++;
  items = [...items, arm({ ...item, id })];
  emit();
  return id;
}

/** Pause auto-dismiss while the user reads or interacts with a toast. */
function hold(id: number): void {
  const item = items.find((entry) => entry.id === id);
  if (!item || item.duration === 0 || !item.timer) return;
  window.clearTimeout(item.timer);
  update(id, { timer: undefined, held: true });
}

/** Resume auto-dismiss once the pointer and focus have left. */
function release(id: number, delay = 4000): void {
  const item = items.find((entry) => entry.id === id);
  if (!item || !item.held) return;
  update(id, { held: false, timer: window.setTimeout(() => dismiss(id), delay) });
}

/** Expand the details list and pin the toast, so it can't vanish while read. */
function expand(id: number): void {
  const item = items.find((entry) => entry.id === id);
  if (!item) return;
  if (item.timer) window.clearTimeout(item.timer);
  update(id, { open: true, held: false, duration: 0, timer: undefined });
}

/**
 * Show a transient toast. Auto-dismisses after `duration` ms (default 5s).
 * Passing a `key` makes repeats refresh one toast, with a count and details.
 */
export function toast(
  message: string,
  kind: ToastKind = 'info',
  duration = 5000,
  options: { key?: string; count?: number; details?: string[] } = {},
): void {
  push({ message, kind, duration, ...options });
}

/**
 * Ask for confirmation with a sticky toast. Resolves true/false; if the user
 * never answers, resolves false after `timeout` ms (default 60s).
 */
export function confirmToast(options: {
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  timeout?: number;
}): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: boolean) => {
      if (settled) return;
      settled = true;
      resolvers.delete(id);
      restoreFocus(id);
      dismiss(id);
      resolve(value);
    };
    const id = push({
      message: options.message,
      kind: options.danger ? 'error' : 'info',
      duration: 0,
      actions: [
        { label: options.confirmLabel ?? 'Confirm', value: true, danger: options.danger },
        { label: options.cancelLabel ?? 'Cancel', value: false },
      ],
    });
    resolvers.set(id, finish);
    window.setTimeout(() => finish(false), options.timeout ?? 60_000);
  });
}

function answer(id: number, value: boolean) {
  resolvers.get(id)?.(value);
}

/**
 * Centered destructive-action confirmation. Unlike confirmToast, this dialog
 * must be answered: no backdrop-dismiss, no Escape, no timeout. Optionally
 * requires typing a phrase (e.g. DELETE) before Confirm enables.
 */
export function confirmModal(options: {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** When set, Confirm stays disabled until the user types this phrase. */
  requireText?: string;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const id = nextId++;
    modalResolvers.set(id, resolve);
    modal = {
      id,
      title: options.title,
      message: options.message,
      confirmLabel: options.confirmLabel ?? 'Confirm',
      cancelLabel: options.cancelLabel ?? 'Cancel',
      requireText: options.requireText,
    };
    emitModal();
  });
}

function answerModal(id: number, value: boolean) {
  if (modal?.id !== id) return;
  modal = undefined;
  modalResolvers.get(id)?.(value);
  modalResolvers.delete(id);
  emitModal();
}

// Focus bookkeeping shared by confirm toasts and the modal.
const priorFocus = new Map<number, HTMLElement>();

function rememberFocus(id: number) {
  const active = document.activeElement;
  if (active instanceof HTMLElement) priorFocus.set(id, active);
}

function restoreFocus(id: number) {
  priorFocus.get(id)?.focus?.();
  priorFocus.delete(id);
}

/** Renders toasts and the confirm modal. Mount once, near the app root. */
export function ToastHost() {
  const [current, setCurrent] = useState<ToastItem[]>([]);
  const [activeModal, setActiveModal] = useState<ModalItem | undefined>();
  const [typed, setTyped] = useState('');
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listeners.add(setCurrent);
    modalListeners.add(setActiveModal);
    return () => {
      listeners.delete(setCurrent);
      modalListeners.delete(setActiveModal);
    };
  }, []);

  // Modal focus management: capture prior focus, move focus to the Cancel
  // button (destructive actions must never be one accidental Enter away),
  // and restore focus when the dialog closes.
  useEffect(() => {
    if (!activeModal) {
      setTyped('');
      return;
    }
    rememberFocus(activeModal.id);
    const focusTimer = window.setTimeout(() => {
      dialogRef.current?.querySelector<HTMLButtonElement>('.confirm-cancel')?.focus();
    });
    return () => {
      window.clearTimeout(focusTimer);
      restoreFocus(activeModal.id);
    };
  }, [activeModal]);

  if (!current.length && !activeModal) return null;

  const typedOk = !activeModal?.requireText || typed === activeModal.requireText;

  return (
    <>
      {activeModal && (
        <div className="confirm-backdrop" role="presentation">
          <div
            ref={dialogRef}
            className="confirm-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-modal-title"
            aria-describedby="confirm-modal-message"
            onKeyDown={(event) => {
              if (event.key === 'Tab') {
                // Focus trap: keep Tab cycling among the dialog's controls.
                const focusables = dialogRef.current?.querySelectorAll<HTMLElement>('button, input');
                if (!focusables?.length) return;
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                  event.preventDefault();
                  last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                  event.preventDefault();
                  first.focus();
                }
              }
            }}
          >
            <h2 id="confirm-modal-title">{activeModal.title}</h2>
            <p id="confirm-modal-message">{activeModal.message}</p>
            {activeModal.requireText && (
              <label className="confirm-type-field">
                <span>
                  Type <b>{activeModal.requireText}</b> to confirm
                </span>
                <input
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
            )}
            <div className="confirm-actions">
              <button type="button" className="confirm-cancel" onClick={() => answerModal(activeModal.id, false)}>
                {activeModal.cancelLabel}
              </button>
              <button
                type="button"
                className="confirm-delete"
                disabled={!typedOk}
                onClick={() => answerModal(activeModal.id, true)}
              >
                {activeModal.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
      {current.length > 0 && (
        <div className="toast-stack" aria-live="polite">
          {current.map((item) => {
            const isConfirm = Boolean(item.actions?.length);
            return (
              <div
                key={item.id}
                className={`toast ${item.kind}${isConfirm ? ' confirm' : ''}${item.open ? ' expanded' : ''}`}
                onMouseEnter={() => hold(item.id)}
                onMouseLeave={() => release(item.id)}
                onFocus={() => hold(item.id)}
                onBlur={() => release(item.id)}
                role={isConfirm ? 'alertdialog' : 'status'}
                aria-modal={isConfirm || undefined}
                aria-label={isConfirm ? item.message : undefined}
                tabIndex={isConfirm ? -1 : undefined}
                ref={
                  isConfirm
                    ? (element) => {
                        if (!element) return;
                        rememberFocus(item.id);
                        // Move focus to the cancel button (rendered last):
                        // confirms must never sit one accidental Enter away
                        // from their confirm action.
                        requestAnimationFrame(() => {
                          const buttons = element.querySelectorAll<HTMLButtonElement>('.toast-button');
                          buttons[buttons.length - 1]?.focus();
                        });
                      }
                    : undefined
                }
                onKeyDown={
                  isConfirm
                    ? (event) => {
                        if (event.key === 'Escape') {
                          event.stopPropagation();
                          answer(item.id, false);
                          return;
                        }
                        if (event.key === 'Tab') {
                          const focusables = event.currentTarget.querySelectorAll<HTMLElement>('.toast-button');
                          if (!focusables.length) return;
                          const first = focusables[0];
                          const last = focusables[focusables.length - 1];
                          if (event.shiftKey && document.activeElement === first) {
                            event.preventDefault();
                            last.focus();
                          } else if (!event.shiftKey && document.activeElement === last) {
                            event.preventDefault();
                            first.focus();
                          }
                        }
                      }
                    : undefined
                }
              >
                <span className="toast-body">
                  <span className="toast-message">
                    {item.message}
                    {item.count !== undefined && item.count > 1 && (
                      <span className="toast-count" title={`${item.count} occurrences`}>
                        ×{item.count}
                      </span>
                    )}
                  </span>
                  {item.details?.length ? (
                    <button
                      type="button"
                      className="toast-details-toggle"
                      aria-expanded={Boolean(item.open)}
                      onClick={() => (item.open ? update(item.id, { open: false }) : expand(item.id))}
                    >
                      {item.open ? 'Hide details' : `Details (${item.details.length})`}
                    </button>
                  ) : null}
                  {item.open && item.details?.length ? (
                    <ul className="toast-details">
                      {item.details.map((detail, index) => (
                        <li key={index}>{detail}</li>
                      ))}
                    </ul>
                  ) : null}
                </span>
                {item.actions && (
                  <span className="toast-actions">
                    {item.actions.map((action) => (
                      <button
                        key={action.label}
                        type="button"
                        className={`toast-button${action.danger ? ' danger' : action.value ? ' primary' : ''}`}
                        onClick={() => answer(item.id, action.value)}
                      >
                        {action.label}
                      </button>
                    ))}
                  </span>
                )}
                {!isConfirm && item.open && (
                  <button type="button" className="toast-button" onClick={() => dismiss(item.id)}>
                    Dismiss
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
