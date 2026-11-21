"use strict";

const $ = (id) => document.getElementById(id);
const routeId = location.pathname.split("/")[2];
const formatDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const writable = document.body.dataset.write === "yes";
let page = null;
let pages = [];
let byId = new Map();
let editing = null;
let dirty = false;
let saving = false;
let createRequest;
let searchTimer;
let previewTimer;
let searchRequest;
let previewRequest;
let historyRequest;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function pageLink(item, className) {
  const link = element("a", item.title, className);
  link.href = "/pages/" + item.id;
  return link;
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").dataset.error = error ? "yes" : "no";
}

async function api(path, { method = "GET", body, signal } = {}) {
  const response = await fetch(path, {
    method, signal,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Nonfluence could not complete this request.");
  return data;
}

function pageTree(matches) {
  const children = new Map();
  for (const item of pages) {
    const siblings = children.get(item.parent_id) || [];
    siblings.push(item);
    children.set(item.parent_id, siblings);
  }
  function list(items, level) {
    const ul = element("ul", undefined, "tree");
    for (const item of items) {
      const li = element("li");
      const link = pageLink(item);
      if (page?.id === item.id) link.setAttribute("aria-current", "page");
      li.append(link);
      if (item.excerpt) li.append(element("small", item.excerpt));
      if (!matches && children.has(item.id) && level < 8) {
        li.append(list(children.get(item.id), level + 1));
      } else if (!matches && children.has(item.id)) {
        const deeper = [];
        const pending = [...children.get(item.id)].reverse();
        while (pending.length) {
          const child = pending.pop();
          deeper.push(child);
          for (const nested of [...(children.get(child.id) || [])].reverse()) pending.push(nested);
        }
        const flat = element("ul", undefined, "tree");
        for (const child of deeper) {
          const childItem = element("li");
          const childLink = pageLink(child);
          if (page?.id === child.id) childLink.setAttribute("aria-current", "page");
          childItem.append(childLink);
          flat.append(childItem);
        }
        li.append(flat);
      }
      ul.append(li);
    }
    return ul;
  }
  $("page-tree").replaceChildren(list(matches || children.get(null) || [], 1));
}

async function search() {
  searchRequest?.abort();
  const query = $("search-input").value.trim();
  $("nav-title").textContent = query ? "Search results" : "Pages";
  const status = $("search-status");
  status.hidden = !query;
  if (!query) {
    pageTree();
    return;
  }
  const request = new AbortController();
  searchRequest = request;
  status.textContent = "Searching…";
  try {
    const { pages: matches } = await api("/api/search?q=" + encodeURIComponent(query), { signal: request.signal });
    if (request.signal.aborted) return;
    pageTree(matches);
    status.textContent = matches.length ? matches.length + (matches.length === 1 ? " result" : " results") : "No matching pages";
  } catch (error) {
    if (!request.signal.aborted) {
      status.textContent = error.message;
      pageTree([]);
    }
  }
}

function ancestors(id) {
  const path = [];
  let next = byId.get(id);
  while (next) {
    path.push(next);
    next = byId.get(next.parent_id);
  }
  return path.reverse();
}

function renderReferences(items) {
  const container = $("references");
  if (!items.length) {
    container.replaceChildren(element("p", "No references", "minor"));
    return;
  }
  const labels = {
    page: "Page", nira: "Nira", nira_hq_project: "Nira project", nira_hq_issue: "Nira ticket",
    github_pull: "GitHub pull request", github_issue: "GitHub issue",
    github_commit: "GitHub commit", github_repo: "GitHub repository",
  };
  const states = { broken: "Broken reference", unavailable: "Unavailable", unverified: "Status unavailable" };
  const list = element("ul");
  for (const ref of items) {
    const card = element("li", undefined, "reference-card");
    const heading = element(ref.url ? "a" : "strong", ref.title);
    if (ref.url) heading.href = ref.url;
    const ticketKey = ref.kind === "nira" ? ref.display_key || ref.target_id : ref.display_key;
    const label = ref.kind === "nira_hq_issue" && ref.type === "epic" ? "Nira epic" : labels[ref.kind];
    const provider = [label, ticketKey, ref.provider === "fixture" ? "Fixture" : null].filter(Boolean);
    card.append(element("div", provider.join(" · "), "minor"), heading);
    const status = ref.status || states[ref.state];
    if (status) card.append(element("p", status, "minor " + ref.state));
    if (ref.preview) card.append(element("p", ref.preview, "minor"));
    list.append(card);
  }
  container.replaceChildren(list);
}

function showPage() {
  $("reader").hidden = !page;
  $("details").hidden = !page;
  $("empty").hidden = Boolean(page);
  if (!page) return;
  document.title = page.title + " · Nonfluence";
  $("page-title").textContent = page.title;
  $("page-meta").textContent = "Revision " + page.revision + " · " + formatDate.format(new Date(page.updated_at));
  $("page-body").innerHTML = page.html;
  const crumb = $("breadcrumbs");
  crumb.replaceChildren(element("span", document.body.dataset.space));
  for (const parent of ancestors(page.parent_id)) {
    crumb.append(element("span", "/"), pageLink(parent));
  }
  renderReferences(page.references);
  const backlinks = $("backlinks");
  backlinks.replaceChildren(...(page.backlinks.length
    ? page.backlinks.map((source) => pageLink(source, "backlink"))
    : [element("p", "No pages link here", "minor")]));
}

async function showHistory() {
  const dialog = $("history-dialog");
  $("history-list").textContent = "Loading history…";
  $("history-preview").replaceChildren();
  dialog.showModal();
  try {
    const { revisions } = await api("/api/pages/" + page.id + "/history");
    const buttons = revisions.map((revision) => {
      const label = "Revision " + revision.revision + " · " + formatDate.format(new Date(revision.updated_at));
      const button = element("button", label, "secondary");
      button.type = "button";
      button.addEventListener("click", async () => {
        historyRequest?.abort();
        const request = new AbortController();
        historyRequest = request;
        try {
          const version = await api("/api/pages/" + page.id + "/history/" + revision.revision, { signal: request.signal });
          if (request.signal.aborted) return;
          const body = element("div");
          body.innerHTML = version.html;
          $("history-preview").replaceChildren(element("h2", version.title), body);
        } catch (error) {
          if (!request.signal.aborted) $("history-preview").textContent = error.message;
        }
      });
      return button;
    });
    $("history-list").replaceChildren(...buttons);
  } catch (error) {
    $("history-list").textContent = error.message;
  }
}

function queuePreview() {
  clearTimeout(previewTimer);
  previewRequest?.abort();
  const markdown = $("editor-source").value;
  if (!markdown.trim()) {
    $("preview-body").replaceChildren();
    return;
  }
  previewTimer = setTimeout(async () => {
    const request = new AbortController();
    previewRequest = request;
    try {
      const { html } = await api("/api/preview", { method: "POST", body: { markdown }, signal: request.signal });
      if (!request.signal.aborted && editing) $("preview-body").innerHTML = html;
    } catch (error) {
      if (!request.signal.aborted) $("preview-body").textContent = error.message;
    }
  }, 180);
}

function startEditor(mode) {
  if (saving || (dirty && !window.confirm("Discard your unsaved changes?"))) return;
  editing = mode;
  dirty = false;
  const selected = mode === "edit" ? page : null;
  createRequest = selected ? null : crypto.randomUUID();
  $("editor-heading").textContent = selected ? "Edit page" : "New page";
  $("editor-title").value = selected?.title || "";
  $("editor-source").value = selected?.markdown || "";
  const selector = $("editor-parent");
  selector.replaceChildren(new Option("Space root", ""));
  for (const candidate of pages) {
    const path = ancestors(candidate.id);
    if (!selected || !path.some((entry) => entry.id === selected.id)) {
      selector.add(new Option(path.map((entry) => entry.title).join(" / "), candidate.id));
    }
  }
  selector.value = selected?.parent_id || "";
  $("reader").hidden = true;
  $("empty").hidden = true;
  $("details").hidden = true;
  $("editor").hidden = false;
  $("editor-title").focus();
  $("preview-body").replaceChildren();
  notice("");
  queuePreview();
}

function cancelEdit() {
  if (saving || (dirty && !window.confirm("Discard your unsaved changes?"))) return;
  clearTimeout(previewTimer);
  previewRequest?.abort();
  dirty = false;
  editing = null;
  $("editor").hidden = true;
  notice("");
  showPage();
}

async function savePage(event) {
  event.preventDefault();
  if (saving) return;
  const selected = editing === "edit" ? page : null;
  const fields = {
    title: $("editor-title").value,
    markdown: $("editor-source").value,
    parent_id: $("editor-parent").value || null,
  };
  if (selected) fields.expected_revision = selected.revision;
  else fields.request_id = createRequest;
  saving = true;
  const controls = ["save-page", "editor-title", "editor-source", "editor-parent"].map($);
  for (const control of controls) control.disabled = true;
  try {
    const saved = await api(selected ? "/api/pages/" + selected.id : "/api/pages", {
      method: selected ? "PUT" : "POST",
      body: fields,
    });
    dirty = false;
    location.assign("/pages/" + saved.id);
  } catch (error) {
    notice(error.message, true);
    for (const control of controls) control.disabled = false;
    saving = false;
  }
}

$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(searchTimer);
  void search();
});
$("search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchRequest?.abort();
  searchTimer = setTimeout(search, 160);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey &&
      !event.target.matches("input, textarea, select, [contenteditable]") && !$("history-dialog").open) {
    event.preventDefault();
    $("search-input").focus();
  }
});
$("history-button").addEventListener("click", showHistory);
$("history-dialog").addEventListener("close", () => historyRequest?.abort());
$("copy-link").addEventListener("click", async () => {
  const label = page.title.replaceAll("\\", "\\\\").replaceAll("[", "\\[").replaceAll("]", "\\]");
  try {
    await navigator.clipboard.writeText("[" + label + "](page:" + page.id + ")");
    notice("Markdown link copied.");
  } catch {
    notice("Clipboard unavailable. Page ID " + page.id, true);
  }
});

if (writable) {
  $("new-page").addEventListener("click", () => startEditor("new"));
  $("edit-page").addEventListener("click", () => startEditor("edit"));
  $("cancel-edit").addEventListener("click", cancelEdit);
  $("editor").addEventListener("input", () => { dirty = true; });
  $("editor-source").addEventListener("input", queuePreview);
  $("editor").addEventListener("submit", savePage);
  window.addEventListener("beforeunload", (event) => {
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

async function initialize() {
  try {
    ({ pages } = await api("/api/pages"));
    byId = new Map(pages.map((item) => [item.id, item]));
    pageTree();
    const initial = routeId || (pages.find((item) => !item.parent_id) || pages[0])?.id;
    if (initial) {
      page = await api("/api/pages/" + initial);
      if (!routeId && location.pathname === "/") history.replaceState(null, "", "/pages/" + page.id);
      if (!editing) showPage();
      if ($("search-input").value.trim()) void search();
      else pageTree();
    } else if (!editing) showPage();
  } catch (error) {
    notice(error.message, true);
  }
}

void initialize();
