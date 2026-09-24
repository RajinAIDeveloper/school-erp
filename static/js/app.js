document.addEventListener("DOMContentLoaded", () => {
  // Mobile sidebar
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  const toggle = document.getElementById("sidebar-toggle");
  const closeSidebar = () => { sidebar.classList.add("-translate-x-full"); backdrop.classList.add("hidden"); };
  toggle?.addEventListener("click", () => {
    sidebar.classList.toggle("-translate-x-full");
    backdrop.classList.toggle("hidden");
  });
  backdrop?.addEventListener("click", closeSidebar);

  // Dropdowns
  document.querySelectorAll("[data-dropdown]").forEach((dd) => {
    const btn = dd.querySelector("[data-dropdown-toggle]");
    const menu = dd.querySelector("[data-dropdown-menu]");
    btn.addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("hidden"); });
    document.addEventListener("click", () => menu.classList.add("hidden"));
  });

  // Dependent selects: <select data-depends-on="#id_class_level" data-url="/academics/sections/?class_level=">
  document.querySelectorAll("select[data-depends-on]").forEach((child) => {
    const parent = document.querySelector(child.dataset.dependsOn);
    if (!parent) return;
    const load = (keepValue) => {
      const current = keepValue ? child.value : "";
      fetch(child.dataset.url + encodeURIComponent(parent.value), { headers: { "X-Requested-With": "fetch" } })
        .then((r) => r.json())
        .then((items) => {
          child.innerHTML = '<option value="">---------</option>';
          items.forEach((it) => {
            const opt = document.createElement("option");
            opt.value = it.id; opt.textContent = it.name;
            if (String(it.id) === current) opt.selected = true;
            child.appendChild(opt);
          });
        });
    };
    parent.addEventListener("change", () => load(false));
    if (parent.value && child.options.length <= 1) load(true);
  });

  // Auto-submit filter forms on change
  document.querySelectorAll("form[data-autosubmit] select, form[data-autosubmit] input[type=date]").forEach((el) => {
    el.addEventListener("change", () => el.form.submit());
  });

  // Mark-all buttons on the attendance register. Scoped to the button's own form so a
  // page with two registers cannot overwrite the wrong one. The "Clear" button carries an
  // empty value, which matches the register's own "Not recorded" option.
  document.querySelectorAll("[data-mark-all]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const scope = btn.closest("form") || document;
      const wanted = btn.dataset.markAll;
      scope.querySelectorAll('input[type=radio]:not([disabled])').forEach((radio) => {
        if (radio.value === wanted) radio.checked = true;
      });
    });
  });

  // Mark a whole class absent, or clear the lot. Scoped to the button's own form, and
  // clearing a student's absence also clears the score box that the absence disabled.
  document.querySelectorAll("[data-check-all]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const scope = btn.closest("form") || document;
      const absent = btn.dataset.checkAll === "absent";
      scope.querySelectorAll("input[data-absent]:not([disabled])").forEach((box) => {
        box.checked = absent;
      });
    });
  });

  // Unsaved work survives a dropped connection. A form with data-draft-key keeps what was
  // typed in this browser until the server has it. data-draft-base names the saved state the
  // draft was typed over: once that changes (the save went through, or someone else saved) the
  // draft is dropped, never replayed over newer marks. Storage can be unavailable (a private
  // window), so every use of it is guarded.
  document.querySelectorAll("form[data-draft-key]").forEach((form) => {
    const key = "draft:" + form.dataset.draftKey;
    const base = form.dataset.draftBase || "";
    const store = {
      read() { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { return null; } },
      write(value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* no storage */ } },
      clear() { try { localStorage.removeItem(key); } catch (e) { /* no storage */ } },
    };
    const values = () => {
      const out = {};
      Array.from(form.elements).forEach((el) => {
        if (!el.name || el.disabled || ["hidden", "submit", "button", "file"].includes(el.type)) return;
        if (el.type === "checkbox") out[el.name] = el.checked;
        else if (el.type === "radio") { if (el.checked) out[el.name] = el.value; }
        else out[el.name] = el.value;
      });
      return out;
    };
    const put = (name, value) => {
      const el = form.elements[name];
      if (!el || el.disabled) return;
      if (el.type === "checkbox") el.checked = value; else el.value = value;
    };
    const initial = JSON.stringify(values());
    let dirty = false;
    let submitting = false;
    const remember = () => {
      const now = values();
      dirty = JSON.stringify(now) !== initial;
      if (dirty) store.write({ base, saved: Date.now(), values: now }); else store.clear();
    };
    form.addEventListener("input", remember);
    form.addEventListener("change", remember);
    form.addEventListener("submit", () => { submitting = true; });
    window.addEventListener("beforeunload", (e) => {
      if (dirty && !submitting) { e.preventDefault(); e.returnValue = ""; }
    });

    const draft = store.read();
    if (!draft || !draft.values) return;
    const current = values();
    const differs = Object.keys(draft.values).some((name) => name in current && current[name] !== draft.values[name]);
    if (draft.base !== base || !differs) { store.clear(); return; }
    const banner = form.querySelector("[data-draft-banner]");
    if (!banner) return;
    const when = banner.querySelector("[data-draft-time]");
    if (when) when.textContent = new Date(draft.saved).toLocaleString();
    banner.classList.remove("hidden");
    banner.querySelector("[data-draft-restore]")?.addEventListener("click", () => {
      Object.entries(draft.values).forEach(([name, value]) => put(name, value));
      banner.classList.add("hidden");
      remember();
    });
    banner.querySelector("[data-draft-discard]")?.addEventListener("click", () => {
      store.clear();
      banner.classList.add("hidden");
    });
  });

  // Signing out leaves no unsaved marks behind on a shared computer.
  document.querySelectorAll("form[data-clear-drafts]").forEach((f) => {
    f.addEventListener("submit", () => {
      try {
        Object.keys(localStorage).filter((k) => k.startsWith("draft:")).forEach((k) => localStorage.removeItem(k));
      } catch (e) { /* no storage */ }
    });
  });

  // Confirm dialogs
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
});
