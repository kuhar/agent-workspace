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
  function rangeBadge(c) {
    if (c.end_line == null || c.end_line === c.line) return "";
    const lo = Math.min(c.line, c.end_line);
    const hi = Math.max(c.line, c.end_line);
    return `<span class="round range">L${lo}–L${hi}</span>`;
  }

  function renderComment(c) {
    const cls = ["comment"];
    if (c.stale) cls.push("stale");
    if (c.resolved) cls.push("resolved");
    const resolveBtn = c.resolved
      ? ""
      : `<button data-resolve="${esc(c.id)}">Resolve</button>`;
    const deleteBtn = `<button class="danger" data-delete="${esc(c.id)}">Delete</button>`;
    return `
      <div class="${cls.join(" ")}" data-cid="${esc(c.id)}">
        <div class="comment-meta">
          <span class="author">${esc(c.author || "unknown")}</span>
          <span class="sev ${esc(c.severity)}">${esc(c.severity)}</span>
          <span class="round">R${c.round}</span>
          ${rangeBadge(c)}
          ${c.stale ? '<span class="round">stale</span>' : ""}
          ${c.resolved ? '<span class="round">resolved</span>' : ""}
          ${resolveBtn}
          ${deleteBtn}
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

  function openForm(file, startLine, startRow, endLine, endRow) {
    // Normalize [startLine, endLine] → [lo, hi]. Thread always anchors at hi,
    // which matches render.py's _group_comments and GitHub's UX regardless of
    // drag direction.
    let lo, hi, anchorRow;
    if (endLine == null) {
      lo = hi = startLine;
      anchorRow = startRow;
    } else {
      lo = Math.min(startLine, endLine);
      hi = Math.max(startLine, endLine);
      anchorRow = hi === startLine ? startRow : endRow;
    }
    const isRange = lo !== hi;
    const label = isRange ? `${file}:${lo}–${hi}` : `${file}:${lo}`;

    const thread = ensureThread(anchorRow, file, hi);
    if (thread.querySelector(".new-comment")) return;  // already open

    // Persist the highlight while the form is open; removed on cancel/submit.
    const highlighted = highlightRange(file, lo, hi);

    const form = document.createElement("div");
    form.className = "new-comment";
    form.innerHTML = `
      <textarea placeholder="Review comment for ${esc(label)}..."></textarea>
      <div class="controls">
        <button class="cancel">Cancel</button>
        <select class="sev">
          <option value="suggestion">suggestion</option>
          <option value="warning">warning</option>
          <option value="critical">critical</option>
          <option value="nit">nit</option>
        </select>
        <button class="submit">Post</button>
      </div>
    `;
    thread.appendChild(form);
    form.querySelector("textarea").focus();

    const cleanup = () => clearRangeHighlight(highlighted);
    const removeFormAndEmptyThread = () => {
      form.remove();
      // If the thread is now empty (no comments, no other form), drop it so
      // we don't leave a blank row between code lines.
      if (!thread.querySelector(".comment") && !thread.querySelector(".new-comment")) {
        thread.remove();
      }
    };
    form.querySelector(".cancel").onclick = () => { cleanup(); removeFormAndEmptyThread(); };
    form.querySelector(".submit").onclick = async () => {
      const body = form.querySelector("textarea").value.trim();
      if (!body) return;
      const severity = form.querySelector(".sev").value;
      const payload = { file, line: lo, body, severity };
      if (isRange) payload.end_line = hi;
      try {
        const c = await api("POST", "/api/comments", payload);
        const rendered = document.createElement("div");
        rendered.innerHTML = renderComment(c);
        thread.insertBefore(rendered.firstElementChild, form);
        cleanup();
        form.remove();
      } catch (e) {
        alert("Post failed: " + e.message);
      }
    };
  }

  // --- Range selection via click-and-drag on gutter line numbers ---
  // Mousedown on .ln starts a drag; mousemove extends the end anchor to any
  // .ln under the cursor (same file only); mouseup opens the form. A plain
  // click (mousedown + mouseup on the same line) produces a single-line form,
  // preserving the prior click-to-comment behaviour.
  let drag = null;  // { file, startLine, startRow, endLine, endRow, highlighted }

  function lineElsBetween(file, lo, hi) {
    const fileEl = document.querySelector(`.file[data-file="${cssEsc(file)}"]`);
    if (!fileEl) return [];
    const out = [];
    for (const el of fileEl.querySelectorAll(".line")) {
      const newLn = el.querySelector(".ln.new");
      const n = newLn ? Number(newLn.dataset.line) : NaN;
      if (Number.isInteger(n) && n >= lo && n <= hi) out.push(el);
    }
    return out;
  }

  function cssEsc(s) {
    // Minimal attribute-selector escape for paths — double quotes and backslashes only.
    return String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function highlightRange(file, lo, hi) {
    const els = lineElsBetween(file, lo, hi);
    for (const el of els) el.classList.add("range-selected");
    return els;
  }

  function clearRangeHighlight(els) {
    if (!els) return;
    for (const el of els) el.classList.remove("range-selected");
  }

  function lnInfo(target) {
    const ln = target.closest(".ln");
    if (!ln || !ln.dataset.line) return null;
    const row = ln.parentElement;
    const fileEl = ln.closest(".file");
    if (!fileEl) return null;
    return {
      file: fileEl.dataset.file,
      line: Number(ln.dataset.line),
      row,
    };
  }

  document.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;  // left button only
    const info = lnInfo(ev.target);
    if (!info) return;
    ev.preventDefault();  // suppress text selection during drag
    document.body.classList.add("gutter-drag");
    drag = {
      file: info.file,
      startLine: info.line,
      startRow: info.row,
      endLine: info.line,
      endRow: info.row,
      highlighted: highlightRange(info.file, info.line, info.line),
    };
  });

  document.addEventListener("mousemove", (ev) => {
    if (!drag) return;
    const info = lnInfo(ev.target);
    if (!info || info.file !== drag.file) return;  // ignore cross-file drags
    if (info.line === drag.endLine) return;
    clearRangeHighlight(drag.highlighted);
    drag.endLine = info.line;
    drag.endRow = info.row;
    const lo = Math.min(drag.startLine, drag.endLine);
    const hi = Math.max(drag.startLine, drag.endLine);
    drag.highlighted = highlightRange(drag.file, lo, hi);
  });

  document.addEventListener("mouseup", (ev) => {
    if (!drag) return;
    document.body.classList.remove("gutter-drag");
    // Clear the drag highlight — openForm re-applies it for the form's lifetime.
    clearRangeHighlight(drag.highlighted);
    const { file, startLine, endLine, startRow, endRow } = drag;
    drag = null;
    if (!file) return;
    if (startLine === endLine) {
      openForm(file, startLine, startRow);
    } else {
      openForm(file, startLine, startRow, endLine, endRow);
    }
  });

  // Resolve / Delete buttons — plain-click handlers, no drag involvement.
  document.addEventListener("click", (ev) => {
    const rb = ev.target.closest("[data-resolve]");
    if (rb) {
      const cid = rb.dataset.resolve;
      api("POST", "/api/resolve", { comment_id: cid }).then(() => {
        const c = rb.closest(".comment");
        c.classList.add("resolved");
        rb.remove();
      }).catch((e) => alert("Resolve failed: " + e.message));
      return;
    }
    const db = ev.target.closest("[data-delete]");
    if (db) {
      const cid = db.dataset.delete;
      if (!confirm("Delete this comment? It's a soft-delete — the record is kept but hidden from agents and the default view.")) return;
      api("POST", "/api/delete", { comment_id: cid }).then(() => {
        const node = db.closest(".comment");
        const thread = node && node.closest(".comment-thread");
        if (node) node.remove();
        // If the thread now has no live comments, drop it entirely.
        if (thread && !thread.querySelector(".comment") && !thread.querySelector(".new-comment")) {
          thread.remove();
        }
      }).catch((e) => alert("Delete failed: " + e.message));
    }
  });

  // --- Live comment merge ---
  // Poll the server for the current (non-deleted) comment set and reconcile
  // against the DOM: insert new ones, remove vanished ones, reflect resolved
  // state changes. Meant to run while agents are posting during a review.

  function findRowForAnchor(fileEl, lineNo) {
    // Match the `.ln.new` (right-gutter) cell that carries the new-file line
    // number, same axis threads are keyed on server-side.
    for (const el of fileEl.querySelectorAll(".line")) {
      const newLn = el.querySelector(".ln.new");
      if (newLn && Number(newLn.dataset.line) === lineNo) return el;
    }
    return null;
  }

  function anchorLineFor(c) {
    return (c.end_line != null && c.end_line !== c.line) ? c.end_line : c.line;
  }

  function insertFetchedComment(c) {
    const fileEl = document.querySelector(`.file[data-file="${cssEsc(c.file)}"]`);
    if (!fileEl) return;  // comment's file isn't in this diff
    const anchor = anchorLineFor(c);
    const row = findRowForAnchor(fileEl, anchor);
    if (!row) return;  // line is outside the diff window; skip
    const thread = ensureThread(row, c.file, anchor);
    const rendered = document.createElement("div");
    rendered.innerHTML = renderComment(c);
    const node = rendered.firstElementChild;
    // Preserve any in-progress composer at the bottom of the thread.
    const form = thread.querySelector(".new-comment");
    if (form) thread.insertBefore(node, form);
    else thread.appendChild(node);
  }

  function pickScrollAnchor() {
    // Topmost element still (partially) on-screen. Prefer `.file` headers
    // because they don't move around as comments insert, but fall back to
    // any visible `.line` so a reviewer mid-scroll inside one file stays
    // pinned to that exact line.
    const candidates = document.querySelectorAll(".file, .line, .comment");
    for (const el of candidates) {
      const r = el.getBoundingClientRect();
      if (r.bottom > 0 && r.top < window.innerHeight) {
        return { el, top: r.top };
      }
    }
    return null;
  }

  function withStableScroll(mutate) {
    const anchor = pickScrollAnchor();
    mutate();
    if (!anchor || !anchor.el.isConnected) return;
    const after = anchor.el.getBoundingClientRect().top;
    const delta = after - anchor.top;
    if (Math.abs(delta) > 0.5) window.scrollBy(0, delta);
  }

  async function refreshComments() {
    let fetched;
    try {
      const r = await fetch(sessionUrl + "/api/comments");
      if (!r.ok) return;
      fetched = await r.json();
    } catch { return; }

    const fetchedById = new Map();
    for (const c of fetched) fetchedById.set(c.id, c);

    const domNodes = document.querySelectorAll(".comment[data-cid]");
    const domIds = new Set();
    for (const el of domNodes) domIds.add(el.dataset.cid);

    // Nothing to change? Skip the scroll dance entirely.
    let anyNew = false;
    for (const c of fetched) if (!domIds.has(c.id)) { anyNew = true; break; }
    let anyGone = false;
    for (const el of domNodes) if (!fetchedById.has(el.dataset.cid)) { anyGone = true; break; }
    let anyStateChange = false;
    for (const el of domNodes) {
      const c = fetchedById.get(el.dataset.cid);
      if (c && c.resolved && !el.classList.contains("resolved")) { anyStateChange = true; break; }
    }
    if (!anyNew && !anyGone && !anyStateChange) return;

    withStableScroll(() => {
      // Removals first — a comment disappearing shifts content upward.
      for (const el of domNodes) {
        if (!fetchedById.has(el.dataset.cid)) {
          const thread = el.closest(".comment-thread");
          el.remove();
          if (thread && !thread.querySelector(".comment") && !thread.querySelector(".new-comment")) {
            thread.remove();
          }
        }
      }
      // State flips on what remains.
      for (const el of document.querySelectorAll(".comment[data-cid]")) {
        const c = fetchedById.get(el.dataset.cid);
        if (!c) continue;
        if (c.resolved && !el.classList.contains("resolved")) {
          el.classList.add("resolved");
          const btn = el.querySelector("[data-resolve]");
          if (btn) btn.remove();
        }
      }
      // Inserts last so new IDs don't collide with nodes we're about to drop.
      for (const c of fetched) {
        if (domIds.has(c.id)) continue;
        insertFetchedComment(c);
      }
    });
  }
  setInterval(refreshComments, 3000);

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
