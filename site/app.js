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

function caseUrl(id) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("case", id);
  else url.searchParams.delete("case");
  url.hash = "cases";
  return url;
}

function setUpCaseList(items, exampleLink) {
  const detailsById = new Map(items.map((item) => [item.id, caseDetails(item)]));
  let active = null;

  function openCase(details) {
    const previous = active;
    active = details;
    if (previous && previous !== details) previous.open = false;
    if (details) details.open = true;
  }

  function selectCase(details) {
    openCase(details);
    const url = caseUrl(details?.dataset.caseId);
    if (url.href !== window.location.href) history.pushState(null, "", url);
  }

  function readLocation() {
    const id = new URLSearchParams(window.location.search).get("case");
    const details = detailsById.get(id) ?? null;
    openCase(details);
    return details;
  }

  for (const details of detailsById.values()) {
    details.querySelector("summary").addEventListener("click", (event) => {
      event.preventDefault();
      selectCase(details.open ? null : details);
    });
  }
  byId("case-list").replaceChildren(...detailsById.values());
  readLocation()?.scrollIntoView({ block: "start" });
  window.addEventListener("popstate", readLocation);

  exampleLink.href = caseUrl(exampleId).href;
  exampleLink.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    const details = detailsById.get(exampleId);
    selectCase(details);
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

load().catch(() => {
  byId("case-load-status").textContent = "The recorded results could not load.";
  byId("show-results").textContent = "Results unavailable";
});
