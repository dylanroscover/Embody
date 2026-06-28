// App-native confirm / prompt for the admin panel -- a styled <dialog> replacing
// window.confirm()/prompt(), so the panel never shows the OS/browser chrome. The
// dialog markup is rendered once in AdminLayout.astro; these helpers drive it and
// resolve a Promise. If the markup is somehow absent, they fall back to native.

interface BaseOpts {
  title?: string;
  message: string;
  okLabel?: string;
  cancelLabel?: string;
  /** Style the OK button as destructive (default true -- most uses are deletes/bans). */
  danger?: boolean;
}

interface PromptOpts extends BaseOpts {
  label?: string;
  placeholder?: string;
  defaultValue?: string;
}

function els() {
  const dialog = document.querySelector<HTMLDialogElement>("[data-admin-dialog]");
  if (!dialog) return null;
  return {
    dialog,
    title: dialog.querySelector<HTMLElement>("[data-admin-dialog-title]"),
    msg: dialog.querySelector<HTMLElement>("[data-admin-dialog-msg]"),
    field: dialog.querySelector<HTMLElement>("[data-admin-dialog-field]"),
    label: dialog.querySelector<HTMLElement>("[data-admin-dialog-label]"),
    input: dialog.querySelector<HTMLInputElement>("[data-admin-dialog-input]"),
    ok: dialog.querySelector<HTMLButtonElement>("[data-admin-dialog-ok]"),
    cancel: dialog.querySelector<HTMLButtonElement>("[data-admin-dialog-cancel]")
  };
}

function open(opts: PromptOpts & { isPrompt: boolean }): Promise<string | null> {
  return new Promise((resolve) => {
    const e = els();
    if (!e) {
      // Markup missing -- degrade to native rather than silently failing.
      if (opts.isPrompt) return resolve(window.prompt(opts.message, opts.defaultValue ?? ""));
      return resolve(window.confirm(opts.message) ? "" : null);
    }

    if (e.title) e.title.textContent = opts.title ?? (opts.isPrompt ? "Confirm" : "Are you sure?");
    if (e.msg) e.msg.textContent = opts.message;
    if (e.ok) {
      e.ok.textContent = opts.okLabel ?? "ok";
      e.ok.classList.toggle("admin-btn--danger", opts.danger !== false);
    }
    if (e.cancel) e.cancel.textContent = opts.cancelLabel ?? "cancel";
    if (e.field) e.field.hidden = !opts.isPrompt;
    if (opts.isPrompt && e.input) {
      e.input.value = opts.defaultValue ?? "";
      e.input.placeholder = opts.placeholder ?? "";
      if (e.label) e.label.textContent = opts.label ?? "";
    }

    let settled = false;
    const done = (val: string | null) => {
      if (settled) return;
      settled = true;
      e.ok?.removeEventListener("click", onOk);
      e.cancel?.removeEventListener("click", onCancel);
      e.dialog.removeEventListener("cancel", onCancel);
      e.input?.removeEventListener("keydown", onKey);
      if (e.dialog.open) e.dialog.close();
      resolve(val);
    };
    const onOk = () => done(opts.isPrompt ? (e.input?.value ?? "") : "");
    const onCancel = (ev?: Event) => {
      ev?.preventDefault();
      done(null);
    };
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        onOk();
      }
    };

    e.ok?.addEventListener("click", onOk);
    e.cancel?.addEventListener("click", onCancel);
    e.dialog.addEventListener("cancel", onCancel); // Esc
    if (opts.isPrompt) e.input?.addEventListener("keydown", onKey);

    e.dialog.showModal();
    if (opts.isPrompt) e.input?.focus();
    else e.ok?.focus();
  });
}

// Resolves true on OK, false on Cancel/Esc.
export function adminConfirm(opts: BaseOpts): Promise<boolean> {
  return open({ ...opts, isPrompt: false }).then((r) => r !== null);
}

// Resolves the entered text on OK ("" if left blank), or null on Cancel/Esc.
export function adminPrompt(opts: PromptOpts): Promise<string | null> {
  return open({ ...opts, isPrompt: true });
}
