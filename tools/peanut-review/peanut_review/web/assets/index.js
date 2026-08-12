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
  function attrEsc(s) {
    return esc(s).replace(/"/g, "&quot;");
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
    const counts = parts.join(" · ");
    const activity = s.push_activity;
    if (!activity || !activity.label) return counts;
    if (activity.status !== "new" && activity.status !== "never") return counts;
    return counts
      + `<div class="push-activity push-${activity.status}" title="GitHub comment push activity">${esc(activity.label)}</div>`;
  }

  function relativeTimeLabel(timestamp) {
    const millis = Date.parse(timestamp);
    if (!Number.isFinite(millis)) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - millis) / 1000));
    if (seconds < 45) return "just now";
    if (seconds < 90) return "1 minute ago";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} minutes ago`;
    if (minutes < 90) return "1 hour ago";
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours !== 1 ? "s" : ""} ago`;
    if (hours < 48) return "yesterday";
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days} days ago`;
    const months = Math.max(1, Math.floor(days / 30));
    if (days < 365) return `${months} month${months !== 1 ? "s" : ""} ago`;
    const years = Math.max(1, Math.floor(days / 365));
    return `${years} year${years !== 1 ? "s" : ""} ago`;
  }

  function updatedCell(timestamp) {
    const label = relativeTimeLabel(timestamp);
    if (!label) return esc(timestamp);
    return `<time class="session-updated" datetime="${esc(timestamp)}" title="${esc(timestamp)}">${esc(label)}</time>`;
  }

  function rowHtml(s) {
    const progress = s.progress || { label: "no review agents", status: "pending" };
    const change = s.change_label || `${s.base_ref} … ${s.topic_ref}`;
    const sessionSubtitle = s.session_subtitle || s.current_head || "";
    const sessionReference = s.github_url
      ? `<a class="github-ref" href="${attrEsc(s.github_url)}" target="_blank" rel="noopener noreferrer" title="Open GitHub PR">${esc(sessionSubtitle)}</a>`
      : esc(sessionSubtitle);
    return `
      <tr class="session-row" data-id="${esc(s.id)}">
        <td class="id"><a href="${BASE}/${esc(s.id)}">${esc(s.id)}</a>
          <div class="mono head">${sessionReference}</div></td>
        <td><span class="badge review-progress progress-${esc(progress.status)}" title="Agent-derived review progress">${esc(progress.label)}</span></td>
        <td class="change" title="${esc(change)}">${esc(change)}</td>
        <td class="mono workspace">${esc(s.workspace)}</td>
        <td class="counts">${countsCell(s)}</td>
        <td class="mono updated">${updatedCell(s.updated_at)}</td>
      </tr>
    `;
  }

  const PAGE_SIZE = 50;
  const pageEtags = new Map();
  let loaded = document.querySelectorAll("#session-rows .session-row").length;
  let activeQuery = "";
  let refreshInFlight = false;

  function applyPage(page, { append = false } = {}) {
    const sessions = Array.isArray(page) ? page : (page.sessions || []);
    const total = Array.isArray(page) ? sessions.length : Number(page.total || 0);
    const tbody = document.getElementById("session-rows");
    if (!tbody) return;
    const rendered = sessions.map(rowHtml).join("");
    if (append) tbody.insertAdjacentHTML("beforeend", rendered);
    else tbody.innerHTML = rendered;
    loaded = tbody.querySelectorAll(".session-row").length;
    const table = tbody.closest("table");
    const empty = document.getElementById("session-empty");
    if (table) table.hidden = loaded === 0;
    if (empty) empty.hidden = loaded !== 0;
    const meta = document.getElementById("session-count");
    if (meta) meta.textContent = `${total} session${total !== 1 ? "s" : ""}`;
    const more = document.getElementById("load-more");
    if (more) more.hidden = Array.isArray(page) || !page.has_more;
  }

  async function fetchPage(offset) {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (activeQuery) params.set("q", activeQuery);
    const url = BASE + "/api/sessions?" + params.toString();
    const headers = {};
    const etag = pageEtags.get(url);
    if (etag) headers["If-None-Match"] = etag;
    const r = await fetch(url, {
      headers,
      cache: "no-store",
    });
    if (r.status === 304) return null;
    if (!r.ok) return null;
    const nextEtag = r.headers.get("ETag");
    if (nextEtag) pageEtags.set(url, nextEtag);
    return r.json();
  }

  async function refresh() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      const page = await fetchPage(0);
      if (page) applyPage(page);
    } catch { /* ignore */ }
    finally { refreshInFlight = false; }
  }

  document.getElementById("refresh")?.addEventListener("click", refresh);
  document.getElementById("load-more")?.addEventListener("click", async () => {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      const page = await fetchPage(loaded);
      if (page) applyPage(page, { append: true });
    } catch { /* ignore */ }
    finally { refreshInFlight = false; }
  });
  let searchTimer = null;
  const sessionSearch = document.getElementById("session-search");
  sessionSearch?.addEventListener("input", (event) => {
    activeQuery = String(event.target.value || "").trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(refresh, 180);
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if (event.key === "Escape" && target === sessionSearch) {
      event.preventDefault();
      sessionSearch.blur();
      return;
    }
    const tagName = target?.tagName;
    const isEditing = target?.isContentEditable
      || tagName === "INPUT"
      || tagName === "TEXTAREA"
      || tagName === "SELECT";
    if (event.key !== "/" || event.defaultPrevented || event.ctrlKey
        || event.metaKey || event.altKey || isEditing || !sessionSearch) return;
    event.preventDefault();
    sessionSearch.focus();
    sessionSearch.select();
  });
  setInterval(refresh, 15000);
})();
