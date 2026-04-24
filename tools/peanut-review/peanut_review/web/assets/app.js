// peanut-review web UI client.
// Comments are rendered on initial page load; this script only handles new
// comment creation, resolving, and periodic session-metadata refresh.

(function () {
  const sessionUrl = window.PR_SESSION_URL;  // set in index template
  const sessionId = window.PR_SESSION_ID;

  // --- Utilities ---
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }
  function api(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(sessionUrl + path, opts).then((r) => {
      if (!r.ok) return r.text().then((t) => { throw new Error(t); });
      return r.json();
    });
  }

  // --- Rendering new comment form / thread ---
  function renderComment(c) {
    const cls = ["comment"];
    if (c.stale) cls.push("stale");
    if (c.resolved) cls.push("resolved");
    const resolveBtn = c.resolved
      ? ""
      : `<button data-resolve="${esc(c.id)}">Resolve</button>`;
    return `
      <div class="${cls.join(" ")}" data-cid="${esc(c.id)}">
        <div class="comment-meta">
          <span class="author">${esc(c.author || "unknown")}</span>
          <span class="sev ${esc(c.severity)}">${esc(c.severity)}</span>
          <span class="round">R${c.round}</span>
          ${c.stale ? '<span class="round">stale</span>' : ""}
          ${c.resolved ? '<span class="round">resolved</span>' : ""}
          ${resolveBtn}
        </div>
        <div class="comment-body">${esc(c.body)}</div>
      </div>
    `;
  }

  function ensureThread(row, file, line) {
    let thread = row.nextElementSibling;
    if (thread && thread.classList.contains("comment-thread") &&
        thread.dataset.file === file && thread.dataset.line === String(line)) {
      return thread;
    }
    thread = document.createElement("div");
    thread.className = "comment-thread";
    thread.dataset.file = file;
    thread.dataset.line = String(line);
    row.insertAdjacentElement("afterend", thread);
    return thread;
  }

  function openForm(file, line, row) {
    const thread = ensureThread(row, file, line);
    if (thread.querySelector(".new-comment")) return;  // already open
    const form = document.createElement("div");
    form.className = "new-comment";
    form.innerHTML = `
      <textarea placeholder="Review comment for ${esc(file)}:${line}..."></textarea>
      <div class="controls">
        <select class="sev">
          <option value="suggestion">suggestion</option>
          <option value="warning">warning</option>
          <option value="critical">critical</option>
          <option value="nit">nit</option>
        </select>
        <span style="flex:1"></span>
        <button class="cancel">Cancel</button>
        <button class="submit">Post</button>
      </div>
    `;
    thread.appendChild(form);
    form.querySelector("textarea").focus();
    form.querySelector(".cancel").onclick = () => form.remove();
    form.querySelector(".submit").onclick = async () => {
      const body = form.querySelector("textarea").value.trim();
      if (!body) return;
      const severity = form.querySelector(".sev").value;
      try {
        const c = await api("POST", "/api/comments", { file, line, body, severity });
        const rendered = document.createElement("div");
        rendered.innerHTML = renderComment(c);
        thread.insertBefore(rendered.firstElementChild, form);
        form.remove();
      } catch (e) {
        alert("Post failed: " + e.message);
      }
    };
  }

  // --- Event wiring ---
  document.addEventListener("click", (ev) => {
    // Line-number click → open form
    const ln = ev.target.closest(".ln");
    if (ln) {
      const file = ln.closest(".file").dataset.file;
      const line = Number(ln.dataset.line);
      if (file && line) openForm(file, line, ln.parentElement);
      return;
    }
    // Resolve button
    const rb = ev.target.closest("[data-resolve]");
    if (rb) {
      const cid = rb.dataset.resolve;
      api("POST", "/api/resolve", { comment_id: cid }).then(() => {
        const c = rb.closest(".comment");
        c.classList.add("resolved");
        rb.remove();
      }).catch((e) => alert("Resolve failed: " + e.message));
    }
  });

  // --- Periodic session refresh (for state/signals) ---
  async function refreshSidebar() {
    try {
      const s = await api("GET", "/api/session");
      const set = (id, val) => {
        const el = document.querySelector(`#sidebar [data-k="${id}"] .v`);
        if (el) el.textContent = val;
      };
      set("state", s.state);
      set("head", (s.current_head || "").slice(0, 12));
      set("stale_comments", s.stale_count);
      if (s.head_shifted) {
        const h = document.querySelector("header .badge.head");
        if (h) { h.textContent = "HEAD shifted"; h.style.background = "#5d4a2a"; }
      }
    } catch { /* ignore */ }
  }
  setInterval(refreshSidebar, 15000);
})();
