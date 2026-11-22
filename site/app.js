const byId = (id) => document.getElementById(id);
const stages = [...document.querySelectorAll("[data-role]")];
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const projectNames = {
  "more-itertools/more-itertools": "more-itertools",
  "pallets-eco/blinker": "Blinker",
  "python-babel/babel": "Babel",
  "python-attrs/attrs": "attrs",
};

let cases = [];
let selectedCase;
let projectFilter = "";
let replayTimer;

function showStage(role) {
  const run = selectedCase.runs.find((candidate) => candidate.role === role);
  const passed = run.result === "pass";

  for (const stage of stages) {
    stage.setAttribute("aria-pressed", String(stage.dataset.role === role));
  }

  byId("stage-result").dataset.result = run.result;
  byId("result-label").textContent = passed ? "Test passed" : "Test failed · " + run.exception;
  const repeated = "Same outcome in " + run.seeds.length + " recorded runs.";
  byId("result-detail").textContent = passed ? selectedCase.expected + " " + repeated : repeated;
}

function stopReplay() {
  window.clearTimeout(replayTimer);
  byId("replay-status").textContent = "";
}

function replay() {
  stopReplay();
  const runs = selectedCase.runs;

  function advance(index) {
    showStage(runs[index].role);
    const step = index + 1;
    if (step === runs.length) {
      byId("replay-status").textContent = "Replay complete";
      return;
    }
    byId("replay-status").textContent = "Showing " + step + " of " + runs.length;
    replayTimer = window.setTimeout(() => advance(step), reducedMotion.matches ? 300 : 650);
  }

  advance(0);
}

function renderCaseLinks() {
  const links = cases
    .filter((item) => !projectFilter || item.repository === projectFilter)
    .map((item) => {
      const row = document.createElement("li");
      const link = document.createElement("a");
      const project = document.createElement("span");
      const title = document.createElement("strong");
      const summary = document.createElement("span");
      const arrow = document.createElement("span");

      link.className = "case-link";
      link.dataset.caseId = item.id;
      if (item === selectedCase) {
        link.setAttribute("aria-current", "true");
      }
      const destination = new URL(window.location.href);
      destination.searchParams.set("case", item.id);
      destination.hash = "demo";
      link.href = destination.href;
      link.addEventListener("click", (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (destination.href !== window.location.href) {
          history.pushState(null, "", destination.href);
        }
        showCase(item);
        byId("demo").focus({ preventScroll: true });
        byId("demo").scrollIntoView({ block: "start" });
      });

      project.className = "case-project";
      project.textContent = projectNames[item.repository] ?? item.repository;
      title.textContent = item.title;
      summary.className = "case-summary";
      summary.textContent = item.summary;
      arrow.className = "case-arrow";
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "↑";
      link.append(project, title, summary, arrow);
      row.append(link);
      return row;
    });

  byId("case-list").replaceChildren(...links);
}

function showCase(item) {
  stopReplay();
  selectedCase = item;
  byId("case-repository").textContent = item.repository;
  byId("case-title").textContent = item.title;
  byId("case-summary").textContent = item.summary;
  byId("reference-code").textContent = item.reference;
  byId("repair-code").textContent = item.repair;
  byId("upstream-notices").textContent = item.license + "\n" + item.notice;
  byId("introducing-pr").href = item.introduced.url;
  byId("fixing-pr").href = item.fixed.url;
  byId("validation-report").href = item.validation_url;
  byId("case-bundle").href = item.bundle_url;
  byId("evidence").open = false;
  byId("notices").open = false;
  for (const stage of stages) {
    const run = item.runs.find((candidate) => candidate.role === stage.dataset.role);
    stage.dataset.result = run.result;
    stage.querySelector(".stage-outcome").textContent = run.result === "pass" ? "Passed" : "Failed";
  }
  showStage("bad");
  renderCaseLinks();
}

function renderFilters() {
  const repositories = [...new Set(cases.map((item) => item.repository))];
  const filters = ["", ...repositories].map((repository) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = repository ? (projectNames[repository] ?? repository) : "All projects";
    button.setAttribute("aria-pressed", String(projectFilter === repository));
    button.addEventListener("click", () => {
      projectFilter = repository;
      for (const filter of byId("case-filters").children) {
        filter.setAttribute("aria-pressed", String(filter === button));
      }
      renderCaseLinks();
    });
    return button;
  });
  byId("case-filters").replaceChildren(...filters);
}

function caseFromLocation() {
  const requested = new URL(window.location.href).searchParams.get("case");
  return cases.find((item) => item.id === requested) ?? cases[0];
}

async function load() {
  byId("load-message").hidden = false;
  const response = await fetch("./data.json");
  if (!response.ok) throw new Error("The recorded data is unavailable.");

  const data = await response.json();
  cases = data.cases;
  showCase(caseFromLocation());
  renderFilters();
  for (const target of document.querySelectorAll("[data-stat]")) {
    target.textContent = data.stats[target.dataset.stat];
  }

  byId("case-controls").hidden = false;
  byId("cases-fallback").hidden = true;
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
  byId("replay").addEventListener("click", replay);
  window.addEventListener("popstate", () => {
    const item = caseFromLocation();
    if (item !== selectedCase) showCase(item);
  });
}

load().catch(() => {
  byId("load-message").textContent = "The recorded results could not load.";
});
