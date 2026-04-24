// peanut-review session index: refresh-on-click + periodic rescan.

(function () {
  const BASE = (typeof window.PR_BASE_URL === "string") ? window.PR_BASE_URL : "";

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

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
    return `
      <tr class="session-row state-${esc(s.state)}" data-id="${esc(s.id)}">
        <td class="id"><a href="${BASE}/${esc(s.id)}">${esc(s.id)}</a>
          <div class="mono head">${esc(s.current_head || "")}</div></td>
        <td><span class="badge state-${esc(s.state)}">${esc(s.state)}</span>
          <div class="sub">${esc(agent)}</div></td>
        <td class="mono refs">${esc(s.base_ref)} … ${esc(s.topic_ref)}</td>
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
