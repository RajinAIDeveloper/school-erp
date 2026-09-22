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

  // Mark-all buttons for attendance grids: <button data-mark-all="present">
  document.querySelectorAll("[data-mark-all]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(`input[type=radio][value="${btn.dataset.markAll}"]`).forEach((r) => (r.checked = true));
    });
  });

  // Confirm dialogs
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
});
