const byId = (id) => document.getElementById(id);
const stages = [...document.querySelectorAll("[data-role]")];
const evidenceButtons = [...document.querySelectorAll("[data-view]")];
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const projectNames = {
  "more-itertools/more-itertools": "more-itertools",
  "pallets-eco/blinker": "Blinker",
  "python-babel/babel": "Babel",
  "python-attrs/attrs": "attrs",
};
const outcomes = {
  pass: { label: "Passed", mark: "✓" },
  fail: { label: "Failed", mark: "×" },
  not_run: { label: "Not run", mark: "–" },
};

let cases = [];
let selectedCase;
let activeView;
let activeRole;
let replayTimer;

function isControl() {
  return activeView === "control";
}

function currentRuns() {
  return isControl() ? selectedCase.control.runs : selectedCase.runs;
}

function showStage(role) {
  activeRole = role;
  const run = currentRuns().find((candidate) => candidate.role === role);
  for (const stage of stages) {
    stage.setAttribute("aria-pressed", String(stage.dataset.role === role));
  }

  let label;
  let detail;
  if (run.result === "not_run") {
    label = "No result for this version";
    detail = "This control wasn’t run on the code before the change.";
  } else if (run.result === "fail") {
    label = "Test failed · " + run.exception;
    detail = "Same failure in " + run.seeds.length + " recorded runs.";
  } else {
    label = "Test passed";
    detail = isControl()
      ? "Passed in all " + run.seeds.length + " recorded runs."
      : selectedCase.expected + " Passed in all " + run.seeds.length + " recorded runs.";
  }
  byId("result-label").textContent = label;
  byId("result-detail").textContent = detail;
}

function stopReplay() {
  window.clearTimeout(replayTimer);
  byId("replay-status").textContent = "";
}

function replay() {
  stopReplay();
  const recorded = currentRuns().filter((run) => run.result !== "not_run");

  function advance(index) {
    showStage(recorded[index].role);
    const step = index + 1;
    if (step === recorded.length) {
      byId("replay-status").textContent = "Replay complete";
      return;
    }
    byId("replay-status").textContent = "Showing " + step + " of " + recorded.length;
    replayTimer = window.setTimeout(() => advance(step), reducedMotion.matches ? 300 : 900);
  }

  advance(0);
}

function diffClass(line) {
  if (/^(diff --git|index |--- |\+\+\+ )/.test(line)) return "diff-meta";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-remove";
  return "";
}

function renderCode(source, isDiff) {
  const lines = source.split("\n");
  if (lines.at(-1) === "") lines.pop();
  const highlighted = lines.map((line, index) => {
    const span = document.createElement("span");
    const highlight = isDiff ? diffClass(line) : "";
    span.className = highlight ? "code-line " + highlight : "code-line";
    span.textContent = line;
    // Keep newline characters so copied code matches the visible lines.
    if (index < lines.length - 1 || source.endsWith("\n")) span.append("\n");
    return span;
  });
  const code = byId("evidence-code");
  code.replaceChildren(...highlighted);
  code.parentElement.scrollTo(0, 0);
}

function showView(view, role = "bad") {
  stopReplay();
  activeView = view;
  const item = selectedCase;
  const evidence = {
    change: ["Original change · PR #" + item.introduced.number, item.change, true],
    reference: ["Historical reference test · Python", item.reference, false],
    control: ["Always-passing control · Python", item.control.source, false],
    repair: ["Production repair · PR #" + item.fixed.number, item.repair, true],
  };
  const [caption, source, isDiff] = evidence[view];

  for (const button of evidenceButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  byId("code-caption").textContent = caption;
  renderCode(source, isDiff);

  const control = isControl();
  byId("results").dataset.mode = control ? "control" : "reference";
  byId("test-kind").textContent = control
    ? "Passing control · " + item.control.score + "/1"
    : "Reference test · 1/1";
  byId("verdict-title").textContent = control ? "Missed the bug" : "Caught the bug";
  byId("verdict-detail").textContent = control
    ? "It passed even when the code was broken."
    : "It failed on the buggy code and passed after the fix.";

  for (const stage of stages) {
    const run = currentRuns().find((candidate) => candidate.role === stage.dataset.role);
    const { label, mark } = outcomes[run.result];
    stage.dataset.result = run.result;
    stage.querySelector(".stage-outcome").textContent = label;
    stage.querySelector(".stage-mark").textContent = mark;
  }
  showStage(role);
}

function caseFromLocation() {
  const requested = new URL(window.location.href).searchParams.get("case");
  return cases.find((item) => item.id === requested) ?? cases[0];
}

function caseUrl(item) {
  const destination = new URL(window.location.href);
  destination.searchParams.set("case", item.id);
  destination.hash = "demo";
  return destination.href;
}

function selectCase(item) {
  const destination = caseUrl(item);
  if (destination !== window.location.href) history.pushState(null, "", destination);
  if (item !== selectedCase) showCase(item);
}

function renderCaseNavigation() {
  const list = byId("case-list");
  const picker = byId("case-select");

  for (const item of cases) {
    const row = document.createElement("li");
    const link = document.createElement("a");
    const project = document.createElement("small");
    const title = document.createElement("strong");
    const projectName = projectNames[item.repository] ?? item.repository;

    link.className = "case-link";
    link.dataset.caseId = item.id;
    link.href = caseUrl(item);
    link.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      selectCase(item);
    });
    project.textContent = projectName;
    title.textContent = item.title;
    link.append(project, title);
    row.append(link);
    list.append(row);

    picker.add(new Option(item.title, item.id));
  }
}

function showCase(item) {
  selectedCase = item;
  byId("case-repository").textContent = item.repository;
  byId("case-title").textContent = item.title;
  byId("case-summary").textContent = item.summary;
  byId("upstream-notices").textContent = item.license + "\n" + item.notice;
  byId("introducing-pr").href = item.introduced.url;
  byId("fixing-pr").href = item.fixed.url;
  byId("validation-report").href = item.validation_url;
  byId("case-bundle").href = item.bundle_url;
  byId("notices").open = false;
  byId("case-select").value = item.id;
  for (const link of document.querySelectorAll("[data-case-id]")) {
    if (link.dataset.caseId === item.id) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  }
  showView("change");
}

async function load() {
  byId("load-message").hidden = false;
  const response = await fetch("./data.json");
  if (!response.ok) throw new Error("The recorded data is unavailable.");

  const data = await response.json();
  cases = data.cases;
  renderCaseNavigation();
  showCase(caseFromLocation());
  for (const target of document.querySelectorAll("[data-stat]")) {
    target.textContent = data.stats[target.dataset.stat];
  }
  byId("stats").hidden = false;
  byId("demo-body").hidden = false;
  byId("demo-loading").hidden = true;
  if (window.location.hash === "#demo") byId("demo").scrollIntoView({ block: "start" });

  for (const stage of stages) {
    stage.addEventListener("click", () => {
      stopReplay();
      showStage(stage.dataset.role);
    });
  }
  for (const button of evidenceButtons) {
    button.addEventListener("click", () => {
      const changesTest = isControl() !== (button.dataset.view === "control");
      showView(button.dataset.view, changesTest ? "bad" : activeRole);
    });
  }
  byId("replay").addEventListener("click", replay);
  byId("case-select").addEventListener("change", (event) => {
    const item = cases.find((candidate) => candidate.id === event.target.value);
    if (item) selectCase(item);
  });
  window.addEventListener("popstate", () => {
    const item = caseFromLocation();
    if (item !== selectedCase) showCase(item);
  });
}

load().catch(() => {
  byId("load-message").textContent = "The recorded results could not load.";
});
