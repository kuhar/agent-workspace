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

  function editedBadge(c) {
    if (!c.edited_at) return "";
    const n = (c.versions || []).length;
    let title = `edited by ${c.edited_by || "unknown"} at ${c.edited_at}`;
    if (n) title += ` (${n} prior version${n === 1 ? "" : "s"})`;
    return `<button class="edited-badge" type="button" data-history="${esc(c.id)}" title="${esc(title)}">edited</button>`;
  }
  function externalLink(c) {
    if (!c.external_url) return "";
    return `<a class="external-link" href="${esc(c.external_url)}" target="_blank" rel="noopener" title="View on GitHub">↗ gh</a>`;
  }

  function renderComment(c, { isReply = false } = {}) {
    const cls = ["comment"];
    if (isReply) cls.push("reply");
    if (c.stale) cls.push("stale");
    if (c.resolved && !isReply) cls.push("resolved");
    if (c.edited_at) cls.push("edited");
    const editBtn = `<button data-edit="${esc(c.id)}">Edit</button>`;
    const deleteBtn = `<button class="danger" data-delete="${esc(c.id)}">Delete</button>`;
    const sevHtml = isReply
      ? ""
      : `<span class="sev ${esc(c.severity)}">${esc(c.severity)}</span>`;
    const resolvedBadge = c.resolved && !isReply ? '<span class="round">resolved</span>' : "";
    return `
      <div class="${cls.join(" ")}" data-cid="${esc(c.id)}">
        <div class="comment-meta">
          <span class="author">${esc(c.author || "unknown")}</span>
          ${sevHtml}
          ${rangeBadge(c)}
          ${c.stale ? '<span class="round">stale</span>' : ""}
          ${resolvedBadge}
          ${editedBadge(c)}
          ${externalLink(c)}
          ${editBtn}
          ${deleteBtn}
        </div>
        <div class="comment-body">${esc(c.body)}</div>
      </div>
    `;
  }

  function renderThreadActions(parentId, resolved) {
    const toggle = resolved
      ? `<button data-unresolve="${esc(parentId)}">Unresolve</button>`
      : `<button data-resolve="${esc(parentId)}">Resolve</button>`;
    return `<div class="thread-actions">
      <button class="reply-btn" data-reply-to="${esc(parentId)}">Reply</button>
      ${toggle}
    </div>`;
  }

  function renderThread(parent) {
    // Initial render only — replies arrive via insertFetchedComment.
    const cls = ["thread"];
    if (parent.resolved) cls.push("resolved");
    return `
      <div class="${cls.join(" ")}" data-thread-id="${esc(parent.id)}">
        ${renderComment(parent)}
        ${renderThreadActions(parent.id, parent.resolved)}
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
        rendered.innerHTML = renderThread(c);
        thread.insertBefore(rendered.firstElementChild, form);
        cleanup();
        form.remove();
      } catch (e) {
        alert("Post failed: " + e.message);
      }
    };
  }

  // --- Reply form (opens at thread bottom, posts with reply_to) ---
  function openReplyForm(threadEl, parentId) {
    if (threadEl.querySelector(".new-comment")) return;
    const actions = threadEl.querySelector(".thread-actions");
    const form = document.createElement("div");
    form.className = "new-comment reply-form";
    form.innerHTML = `
      <textarea placeholder="Reply..."></textarea>
      <div class="controls">
        <button class="cancel">Cancel</button>
        <button class="submit">Reply</button>
      </div>
    `;
    if (actions) threadEl.insertBefore(form, actions);
    else threadEl.appendChild(form);
    form.querySelector("textarea").focus();
    form.querySelector(".cancel").onclick = () => form.remove();
    form.querySelector(".submit").onclick = async () => {
      const body = form.querySelector("textarea").value.trim();
      if (!body) return;
      try {
        const c = await api("POST", "/api/comments",
                            { reply_to: parentId, body });
        const rendered = document.createElement("div");
        rendered.innerHTML = renderComment(c, { isReply: true });
        threadEl.insertBefore(rendered.firstElementChild, form);
        form.remove();
      } catch (e) {
        alert("Reply failed: " + e.message);
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

  // --- Global ("high-level") comment composer ---
  function openGlobalForm() {
    const container = document.getElementById("global-comments");
    if (!container) return;
    if (container.querySelector(".new-comment")) return;  // already open

    const form = document.createElement("div");
    form.className = "new-comment";
    form.innerHTML = `
      <textarea placeholder="High-level feedback (architecture, scope, testing strategy, etc.)..."></textarea>
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
    container.appendChild(form);
    form.querySelector("textarea").focus();

    form.querySelector(".cancel").onclick = () => { form.remove(); };
    form.querySelector(".submit").onclick = async () => {
      const body = form.querySelector("textarea").value.trim();
      if (!body) return;
      const severity = form.querySelector(".sev").value;
      try {
        const c = await api("POST", "/api/comments",
                            { scope: "global", body, severity });
        const rendered = document.createElement("div");
        rendered.innerHTML = renderThread(c);
        container.insertBefore(rendered.firstElementChild, form);
        form.remove();
      } catch (e) {
        alert("Post failed: " + e.message);
      }
    };
  }

  function setThreadResolved(threadEl, resolved) {
    if (!threadEl) return;
    threadEl.classList.toggle("resolved", resolved);
    const parent = threadEl.querySelector(".comment:not(.reply)");
    if (parent) parent.classList.toggle("resolved", resolved);
    // Swap the action button: Resolve <-> Unresolve.
    const actions = threadEl.querySelector(".thread-actions");
    if (actions) {
      const tid = threadEl.dataset.threadId;
      const old = actions.querySelector("[data-resolve], [data-unresolve]");
      if (old) {
        const repl = document.createElement("button");
        if (resolved) {
          repl.dataset.unresolve = tid;
          repl.textContent = "Unresolve";
        } else {
          repl.dataset.resolve = tid;
          repl.textContent = "Resolve";
        }
        old.replaceWith(repl);
      }
    }
    // Toggle the resolved badge on the parent comment-meta.
    if (parent) {
      const meta = parent.querySelector(".comment-meta");
      const badge = meta && meta.querySelector(".round.resolved-badge");
      if (resolved && !badge && meta) {
        const span = document.createElement("span");
        span.className = "round resolved-badge";
        span.textContent = "resolved";
        meta.insertBefore(span, meta.querySelector("button"));
      }
      if (!resolved && badge) badge.remove();
    }
  }

  // Edit + history. Edit replaces the comment-body with a textarea + Save/Cancel.
  // History toggles a panel under the comment showing prior versions inline.
  function applyEditedComment(node, c) {
    const body = node.querySelector(".comment-body");
    if (body) body.textContent = c.body || "";
    if (c.edited_at) node.classList.add("edited");
    const meta = node.querySelector(".comment-meta");
    if (!meta) return;
    let badge = meta.querySelector(".edited-badge");
    const n = (c.versions || []).length;
    let title = `edited by ${c.edited_by || "unknown"} at ${c.edited_at}`;
    if (n) title += ` (${n} prior version${n === 1 ? "" : "s"})`;
    if (!badge) {
      badge = document.createElement("button");
      badge.type = "button";
      badge.className = "edited-badge";
      badge.dataset.history = c.id;
      badge.textContent = "edited";
      const editBtn = meta.querySelector("[data-edit]");
      meta.insertBefore(badge, editBtn || meta.lastElementChild);
    }
    badge.title = title;
    badge.dataset.history = c.id;
    // Stash latest payload on the node so toggleHistory can render without
    // hitting the network — the JSON came back from the POST already.
    node.__prComment = c;
    // Refresh any open history panel.
    const panel = node.querySelector(".version-history");
    if (panel) {
      panel.remove();
      toggleHistory(node, c.id);
    }
  }

  function openEditForm(node, cid) {
    if (node.querySelector(".edit-form")) return;  // already editing
    const body = node.querySelector(".comment-body");
    if (!body) return;
    const current = body.textContent || "";
    const form = document.createElement("form");
    form.className = "edit-form";
    form.innerHTML = `
      <textarea rows="4">${esc(current)}</textarea>
      <div class="edit-actions">
        <button type="submit">Save</button>
        <button type="button" class="cancel">Cancel</button>
      </div>
    `;
    body.style.display = "none";
    body.insertAdjacentElement("afterend", form);
    const ta = form.querySelector("textarea");
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    form.querySelector(".cancel").addEventListener("click", () => {
      form.remove();
      body.style.display = "";
    });
    form.addEventListener("submit", async (sev) => {
      sev.preventDefault();
      const newBody = ta.value;
      if (newBody === current) {
        form.remove();
        body.style.display = "";
        return;
      }
      try {
        const c = await api("POST", "/api/edit", { comment_id: cid, body: newBody });
        form.remove();
        body.style.display = "";
        applyEditedComment(node, c);
      } catch (e) {
        alert("Edit failed: " + e.message);
      }
    });
  }

  async function toggleHistory(node, cid) {
    const existing = node.querySelector(".version-history");
    if (existing) {
      existing.remove();
      return;
    }
    let c = node.__prComment;
    if (!c || c.id !== cid) {
      try {
        const list = await api("GET", "/api/comments?include_deleted=1");
        c = list.find((x) => x.id === cid);
        if (!c) return;
        node.__prComment = c;
      } catch (e) {
        alert("Could not load history: " + e.message);
        return;
      }
    }
    const panel = document.createElement("div");
    panel.className = "version-history";
    const versions = c.versions || [];
    const items = versions.map((v, i) => {
      const ver = i + 1;
      const who = v.edited_by ? esc(v.edited_by) : "original";
      const when = v.edited_at ? ` at ${esc(v.edited_at)}` : "";
      return `<li><div class="vh-meta">v${ver} (${who}${when})</div><pre>${esc(v.body || "")}</pre></li>`;
    }).join("");
    const currentVer = versions.length + 1;
    const curWho = c.edited_by ? esc(c.edited_by) : "current";
    const curWhen = c.edited_at ? ` at ${esc(c.edited_at)}` : "";
    panel.innerHTML = `<ol>${items}<li class="current"><div class="vh-meta">v${currentVer} (${curWho}${curWhen}, current)</div><pre>${esc(c.body || "")}</pre></li></ol>`;
    node.appendChild(panel);
  }

  // Resolve / Unresolve / Delete / Reply — plain-click handlers, no drag involvement.
  document.addEventListener("click", (ev) => {
    if (ev.target.id === "add-global-btn") {
      openGlobalForm();
      return;
    }
    const rb = ev.target.closest("[data-resolve]");
    if (rb) {
      const cid = rb.dataset.resolve;
      api("POST", "/api/resolve", { comment_id: cid }).then(() => {
        setThreadResolved(rb.closest(".thread"), true);
      }).catch((e) => alert("Resolve failed: " + e.message));
      return;
    }
    const ub = ev.target.closest("[data-unresolve]");
    if (ub) {
      const cid = ub.dataset.unresolve;
      api("POST", "/api/unresolve", { comment_id: cid }).then(() => {
        setThreadResolved(ub.closest(".thread"), false);
      }).catch((e) => alert("Unresolve failed: " + e.message));
      return;
    }
    const replyBtn = ev.target.closest("[data-reply-to]");
    if (replyBtn) {
      const pid = replyBtn.dataset.replyTo;
      const threadEl = replyBtn.closest(".thread");
      if (threadEl) openReplyForm(threadEl, pid);
      return;
    }
    const eb = ev.target.closest("[data-edit]");
    if (eb) {
      const cid = eb.dataset.edit;
      const node = eb.closest(".comment");
      if (node) openEditForm(node, cid);
      return;
    }
    const hb = ev.target.closest("[data-history]");
    if (hb) {
      const cid = hb.dataset.history;
      const node = hb.closest(".comment");
      if (node) toggleHistory(node, cid);
      return;
    }
    const db = ev.target.closest("[data-delete]");
    if (db) {
      const cid = db.dataset.delete;
      if (!confirm("Delete this comment? It's a soft-delete — the record is kept but hidden from agents and the default view.")) return;
      api("POST", "/api/delete", { comment_id: cid }).then(() => {
        const node = db.closest(".comment");
        const threadEl = node && node.closest(".thread");
        const anchor = node && node.closest(".comment-thread");
        if (node) node.remove();
        // If we removed the parent comment, drop the entire thread block.
        // If the thread block still has comments (replies remain after a top-
        // level delete is impossible because the parent went, so this branch
        // only applies to reply deletes), keep the actions intact.
        if (threadEl && !threadEl.querySelector(".comment")) threadEl.remove();
        // If the per-line .comment-thread anchor has no surviving threads or
        // forms, drop it so the gap between code lines closes.
        if (anchor && !anchor.querySelector(".comment") &&
            !anchor.querySelector(".new-comment")) {
          anchor.remove();
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

  function findThreadEl(parentId) {
    return document.querySelector(`.thread[data-thread-id="${cssEsc(parentId)}"]`);
  }

  function insertReplyIntoThread(threadEl, c) {
    const rendered = document.createElement("div");
    rendered.innerHTML = renderComment(c, { isReply: true });
    const node = rendered.firstElementChild;
    // Replies sit between the existing replies and the thread-actions; if a
    // reply form is open, drop the reply just above it so the user's
    // in-progress composition stays at the bottom.
    const form = threadEl.querySelector(".new-comment");
    if (form) threadEl.insertBefore(node, form);
    else {
      const actions = threadEl.querySelector(".thread-actions");
      if (actions) threadEl.insertBefore(node, actions);
      else threadEl.appendChild(node);
    }
  }

  function insertFetchedComment(c) {
    if (c.reply_to) {
      const threadEl = findThreadEl(c.reply_to);
      if (threadEl) insertReplyIntoThread(threadEl, c);
      return;
    }
    // Top-level comment — render as a new .thread block in the appropriate
    // container.
    const newThread = document.createElement("div");
    newThread.innerHTML = renderThread(c);
    const threadEl = newThread.firstElementChild;
    if (!c.file) {
      const container = document.getElementById("global-comments");
      if (!container) return;
      const composer = container.querySelector(".new-comment");
      if (composer) container.insertBefore(threadEl, composer);
      else container.appendChild(threadEl);
      return;
    }
    const fileEl = document.querySelector(`.file[data-file="${cssEsc(c.file)}"]`);
    if (!fileEl) return;
    const anchor = anchorLineFor(c);
    const row = findRowForAnchor(fileEl, anchor);
    if (!row) return;
    const anchorContainer = ensureThread(row, c.file, anchor);
    const composer = anchorContainer.querySelector(":scope > .new-comment");
    if (composer) anchorContainer.insertBefore(threadEl, composer);
    else anchorContainer.appendChild(threadEl);
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

  function renderCountsHTML(open, total) {
    if (open > 0) {
      return `<span class="count open">${open}</span>` +
             `<span class="count muted">/${total}</span>`;
    }
    if (total > 0) return `<span class="count muted">${total}</span>`;
    return `<span class="count empty">—</span>`;
  }

  function updateFileCounts(fetched) {
    const total = new Map();
    const open = new Map();
    let globalTotal = 0, globalOpen = 0;
    for (const c of fetched) {
      if (c.reply_to) continue;  // replies don't inflate the open badge
      if (!c.file) {
        globalTotal++;
        if (!c.resolved) globalOpen++;
        continue;
      }
      total.set(c.file, (total.get(c.file) || 0) + 1);
      if (!c.resolved) open.set(c.file, (open.get(c.file) || 0) + 1);
    }
    for (const li of document.querySelectorAll('#sidebar ul.files li.file-row')) {
      const cell = li.querySelector('[data-counts]');
      if (!cell) continue;
      if (li.dataset.global) {
        cell.innerHTML = renderCountsHTML(globalOpen, globalTotal);
      } else {
        const file = li.dataset.file;
        cell.innerHTML = renderCountsHTML(open.get(file) || 0, total.get(file) || 0);
      }
    }
  }

  async function refreshComments() {
    let fetched;
    try {
      const r = await fetch(sessionUrl + "/api/comments");
      if (!r.ok) return;
      fetched = await r.json();
    } catch { return; }

    updateFileCounts(fetched);

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
      if (!c) continue;
      const threadEl = el.closest(".thread");
      if (!threadEl) continue;
      const threadResolved = threadEl.classList.contains("resolved");
      // Top-level comments drive the thread's resolved state; ignore reply
      // resolved flags (they shouldn't be set in practice but defensively
      // we don't want them to flicker the UI).
      if (!c.reply_to && c.resolved !== threadResolved) {
        anyStateChange = true; break;
      }
    }
    if (!anyNew && !anyGone && !anyStateChange) return;

    withStableScroll(() => {
      // Removals first — a comment disappearing shifts content upward.
      for (const el of domNodes) {
        if (!fetchedById.has(el.dataset.cid)) {
          const threadEl = el.closest(".thread");
          const anchor = el.closest(".comment-thread");
          el.remove();
          // If we removed the parent (only top-levels can fully empty a
          // .thread), drop the empty thread block.
          if (threadEl && !threadEl.querySelector(".comment")) threadEl.remove();
          if (anchor && !anchor.querySelector(".comment") &&
              !anchor.querySelector(".new-comment")) {
            anchor.remove();
          }
        }
      }
      // State flips on what remains — toggle thread.resolved class + button.
      for (const el of document.querySelectorAll(".comment[data-cid]")) {
        const c = fetchedById.get(el.dataset.cid);
        if (!c || c.reply_to) continue;
        const threadEl = el.closest(".thread");
        if (!threadEl) continue;
        if (c.resolved !== threadEl.classList.contains("resolved")) {
          setThreadResolved(threadEl, !!c.resolved);
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

  // --- Inbox transcript (agent ask/reply) ---
  // Read-only. Polled on the same 3s cadence as comments so a reviewer
  // watching the page sees blocking questions appear and replies land
  // without manual refresh.
  function renderInboxEntry(entry) {
    const agent = esc(entry.agent || "");
    const qid = esc(entry.id || "");
    const qts = esc(entry.timestamp || "");
    const qtext = esc(entry.question || "");
    let replyHtml;
    if (entry.reply) {
      const ats = esc(entry.reply.timestamp || "");
      const aby = esc(entry.reply.answered_by || "orchestrator");
      const atext = esc(entry.reply.answer || "");
      replyHtml = `<div class="ix-r">
          <span class="ix-meta">
            <span class="agent">↳ ${aby}</span>
            <span class="ts mono">${ats}</span>
          </span>
          <pre class="ix-body">${atext}</pre>
        </div>`;
    } else {
      replyHtml = `<div class="ix-r pending">
          <span class="ix-meta"><span class="agent">↳ awaiting reply…</span></span>
        </div>`;
    }
    return `<div class="ix-entry" data-qid="${qid}">
        <div class="ix-q">
          <span class="ix-meta">
            <span class="agent">${agent}</span>
            <span class="qid mono">${qid}</span>
            <span class="ts mono">${qts}</span>
          </span>
          <pre class="ix-body">${qtext}</pre>
        </div>
        ${replyHtml}
      </div>`;
  }

  function inboxKey(entry) {
    // Question id is unique only within an agent — combine with agent name.
    return `${entry.agent}/${entry.id}`;
  }

  function entryReplied(entry) {
    return entry.reply ? 1 : 0;
  }

  async function refreshInbox() {
    const list = document.getElementById("inbox-list");
    if (!list) return;
    let fetched;
    try {
      const r = await fetch(sessionUrl + "/api/inbox");
      if (!r.ok) return;
      fetched = await r.json();
    } catch { return; }

    // Snapshot current DOM state keyed by qid + reply-flag so we can detect:
    //   - new entries (insert)
    //   - vanished entries (remove — rare, only via manual file deletion)
    //   - reply landed on a previously-pending entry (replace that node)
    const fetchedByKey = new Map();
    for (const e of fetched) fetchedByKey.set(inboxKey(e), e);
    const domByKey = new Map();
    for (const el of list.querySelectorAll(".ix-entry")) {
      const agent = el.parentElement && el.dataset.qid;
      // Reconstruct the same key the server would emit. We store agent on
      // the entry's first child via the ix-meta .agent text — that's
      // brittle; instead we attach data-key directly. Fall back to qid only.
      const key = el.dataset.key || el.dataset.qid;
      domByKey.set(key, el);
    }

    // Cheap no-op short-circuit.
    let dirty = fetched.length !== domByKey.size;
    if (!dirty) {
      for (const [key, e] of fetchedByKey) {
        const el = domByKey.get(key);
        if (!el) { dirty = true; break; }
        const had = el.dataset.replied === "1";
        if (had !== !!entryReplied(e)) { dirty = true; break; }
      }
    }
    if (!dirty) return;

    withStableScroll(() => {
      // Rebuild the list in fetched order. Cheap because the count is
      // small (one entry per agent question). Stable-scroll handles the
      // reflow so reviewers reading mid-page don't jump.
      list.innerHTML = "";
      for (const e of fetched) {
        const wrap = document.createElement("div");
        wrap.innerHTML = renderInboxEntry(e);
        const node = wrap.firstElementChild;
        node.dataset.key = inboxKey(e);
        node.dataset.replied = entryReplied(e) ? "1" : "0";
        list.appendChild(node);
      }
    });
  }
  setInterval(refreshInbox, 3000);

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
