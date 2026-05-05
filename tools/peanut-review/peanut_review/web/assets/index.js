// peanut-review session index: refresh-on-click + periodic rescan.

(function () {
  const BASE = (typeof window.PR_BASE_URL === "string") ? window.PR_BASE_URL : "";
  const THEME_KEY = "pr.theme";
  const THEMES = [
    { value: "system", label: "system" },
    { value: "dark-plus", label: "Dark+" },
    { value: "light", label: "light" },
  ];

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function sessionStateLabel(state) {
    const labels = {
      init: "ready",
      round: "in review",
      complete: "done",
      aborted: "aborted",
    };
    return labels[state] || String(state || "").replace(/-/g, " ");
  }
  function themeConfig(value) {
    return THEMES.find((t) => t.value === value) || THEMES[0];
  }
  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "system";
    } catch {
      return "system";
    }
  }
  function applyTheme(value) {
    const theme = themeConfig(value);
    if (theme.value === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.dataset.theme = theme.value;
    }
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = `theme: ${theme.label}`;
      btn.title = `Color theme: ${theme.label}`;
      btn.dataset.theme = theme.value;
    }
  }
  function setStoredTheme(value) {
    const theme = themeConfig(value);
    try {
      if (theme.value === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme.value);
    } catch { /* ignore */ }
    applyTheme(theme.value);
  }
  function cycleTheme() {
    const current = themeConfig(storedTheme()).value;
    const idx = THEMES.findIndex((t) => t.value === current);
    setStoredTheme(THEMES[(idx + 1) % THEMES.length].value);
  }
  applyTheme(storedTheme());
  document.getElementById("theme-toggle")?.addEventListener("click", cycleTheme);

  function countsCell(s) {
    const parts = [
      `<span class="n">${s.comment_count}</span><span class="sub"> total</span>`,
    ];
    if (s.unresolved_count)
      parts.push(`<span class="n warn">${s.unresolved_count}</span><span class="sub"> open</span>`);
    if (s.critical_count)
      parts.push(`<span class="n crit">${s.critical_count}</span><span class="sub"> crit</span>`);
    if (s.stale_count)
      parts.push(`<span class="n muted">${s.stale_count}</span><span class="sub"> stale</span>`);
    return parts.join(" · ");
  }

  function rowHtml(s) {
    const agent = `${s.agent_count} agent${s.agent_count !== 1 ? "s" : ""}`;
    const change = s.change_label || `${s.base_ref} … ${s.topic_ref}`;
    const sessionSubtitle = s.session_subtitle || s.current_head || "";
    return `
      <tr class="session-row state-${esc(s.state)}" data-id="${esc(s.id)}">
        <td class="id"><a href="${BASE}/${esc(s.id)}">${esc(s.id)}</a>
          <div class="mono head">${esc(sessionSubtitle)}</div></td>
        <td><span class="badge state-${esc(s.state)}" title="session state: ${esc(s.state)}">${esc(sessionStateLabel(s.state))}</span>
          <div class="sub">${esc(agent)}</div></td>
        <td class="change" title="${esc(change)}">${esc(change)}</td>
        <td class="mono workspace">${esc(s.workspace)}</td>
        <td class="counts">${countsCell(s)}</td>
        <td class="mono created">${esc(s.created_at)}</td>
      </tr>
    `;
  }

  async function refresh() {
    try {
      const r = await fetch(BASE + "/api/sessions");
      if (!r.ok) return;
      const sessions = await r.json();
      const tbody = document.getElementById("session-rows");
      if (!tbody) {
        // Empty-state → full reload so server renders the index view again.
        location.reload();
        return;
      }
      if (!sessions.length) { location.reload(); return; }
      tbody.innerHTML = sessions.map(rowHtml).join("");
      const meta = document.querySelector("header .meta:not(.mono)");
      if (meta) meta.textContent = `${sessions.length} session${sessions.length !== 1 ? "s" : ""}`;
    } catch { /* ignore */ }
  }

  document.getElementById("refresh")?.addEventListener("click", refresh);
  setInterval(refresh, 15000);
})();
