const exampleId = "babel-bef3a734fbdf-de44e185-e4a8ca4b";
const projectNames = {
  "more-itertools/more-itertools": "more-itertools",
  "pallets-eco/blinker": "Blinker",
  "python-babel/babel": "Babel",
  "python-attrs/attrs": "attrs",
};
const versionNames = {
  base: "Before the change",
  bad: "With the bug",
  repaired: "With the fix",
};

const byId = (id) => document.getElementById(id);
const views = new Map([...document.querySelectorAll("[data-view]")].map((view) => [view.id, view]));
const viewLinks = [...document.querySelectorAll("[data-view-link]")];
const caseList = byId("case-list");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function codeFigure(label, source) {
  const figure = element("figure", "case-code");
  const pre = element("pre");
  pre.tabIndex = 0;
  pre.setAttribute("aria-label", label);
  pre.append(element("code", "", source));
  figure.append(element("figcaption", "", label), pre);
  return figure;
}

function caseEvidence(item) {
  const evidence = element("details", "code-details");
  const sources = element("ul", "case-sources");
  for (const [label, url] of [
    ["Original pull request", item.introduced.url],
    ["Fix pull request", item.fixed.url],
    ["Validation report", item.validation_url],
    ["Case files", item.bundle_url],
  ]) {
    const link = element("a", "", label);
    link.href = url;
    const entry = element("li");
    entry.append(link);
    sources.append(entry);
  }

  const notices = element("details", "case-notices");
  const noticeText = element("pre", "", item.license + "\n" + item.notice);
  noticeText.tabIndex = 0;
  notices.append(element("summary", "", "Upstream license and notices"), noticeText);

  evidence.append(
    element("summary", "", "View code and sources"),
    sources,
    codeFigure("Original change", item.change),
    codeFigure("Reference test", item.reference),
    codeFigure("Repair", item.repair),
    notices,
  );
  return evidence;
}

function caseDetails(item) {
  const details = element("details", "case-item");
  details.dataset.caseId = item.id;
  const summary = element("summary");
  summary.append(
    element("span", "case-name", item.title),
    element("span", "case-project", projectNames[item.repository] ?? item.repository),
  );

  const runs = element("dl", "case-runs");
  for (const run of item.runs) {
    const row = element("div");
    const result = element("dd", "", run.result === "pass" ? "Passed" : "Failed");
    result.dataset.result = run.result;
    row.append(element("dt", "", versionNames[run.role]), result);
    runs.append(row);
  }

  const expected = element("p", "case-expected");
  expected.append(element("strong", "", "Expected behavior"), " ", item.expected);
  const body = element("div", "case-body");
  body.append(element("p", "", item.summary), expected, runs, caseEvidence(item));
  details.append(summary, body);
  return details;
}

function viewUrl(viewId, caseId) {
  const url = new URL(window.location.href);
  url.hash = viewId;
  if (caseId) url.searchParams.set("case", caseId);
  else url.searchParams.delete("case");
  return url;
}

function renderRoute() {
  const url = new URL(window.location.href);
  const caseId = url.searchParams.get("case");
  const viewId = url.hash.slice(1);
  // Older case links use #demo, and links without a fragment also share the case query.
  const caseRoute = caseId !== null && (viewId === "demo" || !views.has(viewId));
  const activeView = views.get(caseRoute ? "cases" : viewId) ?? views.get("demo");

  for (const view of views.values()) view.hidden = view !== activeView;
  for (const link of viewLinks) {
    const destination = link.hash.slice(1);
    link.href = viewUrl(destination).href;
    if (destination === activeView?.id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const details of caseList.children) {
    details.open = activeView?.id === "cases" && details.dataset.caseId === caseId;
  }
  return activeView;
}

function navigate(viewId, caseId) {
  const url = viewUrl(viewId, caseId);
  if (url.href !== window.location.href) history.pushState(null, "", url);
  return renderRoute();
}

function ordinaryClick(event) {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

function focusView(view) {
  view?.querySelector("h1, h2")?.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function setUpNavigation() {
  for (const link of viewLinks) {
    link.addEventListener("click", (event) => {
      if (!ordinaryClick(event)) return;
      event.preventDefault();
      focusView(navigate(link.hash.slice(1)));
    });
  }
  document.querySelector(".skip-link")?.addEventListener("click", (event) => {
    if (!ordinaryClick(event)) return;
    event.preventDefault();
    focusView(renderRoute());
  });
  window.addEventListener("popstate", renderRoute);
  window.addEventListener("hashchange", renderRoute);
  renderRoute();
}

function setUpCaseList(items, exampleLink) {
  const detailsById = new Map(items.map((item) => [item.id, caseDetails(item)]));
  for (const details of detailsById.values()) {
    details.querySelector("summary").addEventListener("click", (event) => {
      event.preventDefault();
      navigate("cases", details.open ? null : details.dataset.caseId);
    });
  }
  caseList.replaceChildren(...detailsById.values());
  renderRoute();
  caseList.querySelector("[open]")?.scrollIntoView({ block: "start" });

  exampleLink.href = viewUrl("cases", exampleId).href;
  exampleLink.addEventListener("click", (event) => {
    if (!ordinaryClick(event)) return;
    event.preventDefault();
    const details = detailsById.get(exampleId);
    navigate("cases", exampleId);
    details.querySelector("summary").focus({ preventScroll: true });
    details.scrollIntoView({ block: "start" });
  });
}

function setUpExample(example, button) {
  const results = byId("example-results");
  const collapsedLabel = button.textContent.trim();
  for (const [prefix, runs] of [
    ["control", example.control.runs],
    ["reference", example.runs],
  ]) {
    for (const [version, role] of [["buggy", "bad"], ["fixed", "repaired"]]) {
      const run = runs.find((entry) => entry.role === role);
      const cell = byId(prefix + "-" + version);
      cell.textContent = run.result === "pass" ? "Passes" : "Fails";
      cell.dataset.result = run.result;
    }
  }

  button.addEventListener("click", () => {
    const expanded = results.hidden;
    results.hidden = !expanded;
    byId("translation-output").hidden = expanded;
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = expanded ? "Hide test results" : collapsedLabel;
  });
  button.disabled = false;
}

async function load() {
  const response = await fetch("./data.json");
  if (!response.ok) throw new Error("The recorded data is unavailable.");

  const data = await response.json();
  const example = data.cases.find((item) => item.id === exampleId);
  setUpCaseList(data.cases, byId("example-case-link"));
  setUpExample(example, byId("show-results"));
  byId("case-fallback").hidden = true;
}

setUpNavigation();
load().catch(() => {
  byId("case-load-status").textContent = "The recorded results could not load.";
  byId("show-results").textContent = "Results unavailable";
});
