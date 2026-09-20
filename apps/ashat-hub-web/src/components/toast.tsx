import { useEffect, useState } from 'react';

/**
 * Shared toast system.
 *
 * `toast()` shows a transient notification. `confirmToast()` is the
 * non-blocking replacement for window.confirm: it shows a sticky toast with
 * Confirm/Cancel actions and resolves the promise with the answer. The
 * <ToastHost /> component renders the stack; mount it once near the app root.
 */

export type ToastKind = 'info' | 'success' | 'error';

type ToastAction = { label: string; value: boolean; danger?: boolean };

type ToastItem = {
  id: number;
  message: string;
  kind: ToastKind;
  actions?: ToastAction[];
  duration: number;
};

let nextId = 1;
let items: ToastItem[] = [];
const resolvers = new Map<number, (value: boolean) => void>();
const listeners = new Set<(items: ToastItem[]) => void>();

function emit() {
  const snapshot = [...items];
  for (const listener of listeners) listener(snapshot);
}

function dismiss(id: number) {
  items = items.filter((item) => item.id !== id);
  emit();
}

function push(item: Omit<ToastItem, 'id'>): number {
  const id = nextId++;
  items = [...items, { ...item, id }];
  emit();
  if (item.duration > 0) {
    window.setTimeout(() => dismiss(id), item.duration);
  }
  return id;
}

/** Show a transient toast. Auto-dismisses after `duration` ms (default 5s). */
export function toast(message: string, kind: ToastKind = 'info', duration = 5000): void {
  push({ message, kind, duration });
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

/** Renders every active toast. Mount once, near the app root. */
export function ToastHost() {
  const [current, setCurrent] = useState<ToastItem[]>([]);

  useEffect(() => {
    listeners.add(setCurrent);
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);

  if (!current.length) return null;

  return (
    <div className="toast-stack" aria-live="polite">
      {current.map((item) => (
        <div key={item.id} className={`toast ${item.kind}`} role="status">
          <span className="toast-message">{item.message}</span>
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
        </div>
      ))}
    </div>
  );
}
