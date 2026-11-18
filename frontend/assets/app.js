const app = document.querySelector("#app");

const state = {
  agents: [],
  busy: false,
  error: "",
  meeting: null,
  meetings: [],
  mission: null,
  missions: [],
  providers: [],
  selectedMeetingId: null,
  selectedMissionId: null,
  socketConnected: false,
};

const seatName = (agentId) =>
  state.agents.find((agent) => agent.agent_id === agentId)?.name ?? agentId;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function listMarkup(values, emptyText = "None recorded") {
  if (!values?.length) {
    return `<p class="muted small">${escapeHtml(emptyText)}</p>`;
  }
  return `<ul>${values.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const result = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail = typeof result === "object" ? result.detail : result;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return result;
}

async function loadWorkspace() {
  const [missionsResult, providersResult, agentsResult] = await Promise.all([
    api("/api/missions"),
    api("/api/providers"),
    api("/api/agents"),
  ]);
  state.missions = missionsResult.missions;
  state.providers = providersResult.providers;
  state.agents = agentsResult.agents;

  if (!state.missions.some((mission) => mission.mission_id === state.selectedMissionId)) {
    state.selectedMissionId = state.missions[0]?.mission_id ?? null;
    state.selectedMeetingId = null;
  }
  state.mission = null;
  state.meetings = [];
  state.meeting = null;

  if (state.selectedMissionId) {
    const [mission, meetingsResult] = await Promise.all([
      api(`/api/missions/${encodeURIComponent(state.selectedMissionId)}`),
      api(`/api/missions/${encodeURIComponent(state.selectedMissionId)}/meetings`),
    ]);
    state.mission = mission;
    state.meetings = meetingsResult.meetings;
    if (!state.meetings.some((meeting) => meeting.meeting_id === state.selectedMeetingId)) {
      state.selectedMeetingId = null;
    }
    if (state.selectedMeetingId) {
      state.meeting = await api(
        `/api/meetings/${encodeURIComponent(state.selectedMeetingId)}`,
      );
    }
  }
}

function renderProviderStatus() {
  if (!state.providers.length) {
    return `<span class="pill offline">AI provider status unavailable</span>`;
  }
  const ready = state.providers.filter(
    (provider) => provider.available && provider.model_available,
  ).length;
  const providerClass = ready === state.providers.length ? "connected" : "offline";
  const providerLabel = ready === state.providers.length
    ? `AI providers ready · ${ready}/${state.providers.length}`
    : `AI providers unavailable · ${ready}/${state.providers.length}`;
  return `
    <span class="pill ${providerClass}" title="${escapeHtml(
      state.providers.map((provider) => `${provider.provider} ${provider.model}: ${provider.detail}`).join("; "),
    )}">${providerLabel}</span>`;
}

function renderMissionSidebar() {
  const missions = state.missions.length
    ? state.missions.map((mission) => `
      <button
        class="mission-item ${mission.mission_id === state.selectedMissionId ? "selected" : ""}"
        data-action="select-mission"
        data-id="${escapeHtml(mission.mission_id)}"
        type="button"
      >
        <strong>${escapeHtml(mission.name)}</strong>
        <span>${escapeHtml(mission.status)} · ${escapeHtml(mission.mission_id)}</span>
      </button>`).join("")
    : `<div class="empty-state">No missions yet. Create one to begin a planning session.</div>`;

  return `
    <aside class="sidebar">
      <section>
        <div class="section-heading">
          <h2>Missions</h2>
          <span class="badge">${state.missions.length}</span>
        </div>
        <div class="mission-list">${missions}</div>
      </section>
      <form class="mission-create" data-form="create-mission">
        <h3>New mission</h3>
        <label>Mission ID
          <input name="mission_id" maxlength="128" required placeholder="training-01">
        </label>
        <label>Mission name
          <input name="name" maxlength="256" required placeholder="Training exercise">
        </label>
        <label>Objective
          <textarea name="objective" maxlength="4096" required placeholder="Describe the planning objective"></textarea>
        </label>
        <button class="primary full-width" type="submit" ${state.busy ? "disabled" : ""}>Create mission</button>
      </form>
    </aside>`;
}

function statusOptions(current) {
  const allowed = {
    PLANNING: ["PLANNING", "ACTIVE", "ABORTED"],
    ACTIVE: ["ACTIVE", "COMPLETED", "ABORTED"],
    COMPLETED: ["COMPLETED"],
    ABORTED: ["ABORTED"],
  };
  return (allowed[current] ?? [current]).map((status) =>
    `<option value="${status}" ${status === current ? "selected" : ""}>${status}</option>`,
  ).join("");
}

function operatorLines(operators) {
  return (operators ?? []).map((operator) => [
    operator.operator_id,
    operator.status ?? "",
    operator.assignment ?? "",
    operator.available == null ? "" : String(operator.available),
  ].join(" | ")).join("\n");
}

function renderMissionPanel() {
  if (!state.mission) {
    return `
      <section class="panel">
        <p class="section-kicker">Mission workspace</p>
        <h2>Select or create a mission</h2>
        <p class="muted">Mission data is entered by the operator. No game state is being read.</p>
      </section>`;
  }

  const mission = state.mission;
  const events = [...mission.events].reverse().slice(0, 5);
  return `
    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="section-kicker">Mission workspace · ${escapeHtml(mission.mission_id)}</p>
          <h2>${escapeHtml(mission.name)}</h2>
        </div>
        <span class="badge">${escapeHtml(mission.status)}</span>
      </div>
      <form class="form-grid" data-form="update-mission">
        <label>Objective
          <textarea name="objective" maxlength="4096" required>${escapeHtml(mission.objective)}</textarea>
        </label>
        <div class="field-grid">
          <label>Mission status
            <select name="status">${statusOptions(mission.status)}</select>
          </label>
          <label>Operators <small>One per line: id | status | assignment | available (true/false)</small>
            <textarea name="operators" placeholder="operator-1 | ready | observer | true">${escapeHtml(operatorLines(mission.operators))}</textarea>
          </label>
        </div>
        <div class="field-grid">
          <label>Available equipment <small>One item per line</small>
            <textarea name="available_equipment">${escapeHtml((mission.available_equipment ?? []).join("\n"))}</textarea>
          </label>
          <label>Confirmed threats <small>New entries append to history</small>
            <textarea name="known_threats">${escapeHtml((mission.known_threats ?? []).join("\n"))}</textarea>
          </label>
        </div>
        <div class="field-grid">
          <label>Unknown information <small>Keep unconfirmed information explicitly uncertain</small>
            <textarea name="unknown_information">${escapeHtml((mission.unknown_information ?? []).join("\n"))}</textarea>
          </label>
          <label>Observations <small>One observation per line</small>
            <textarea name="observations">${escapeHtml((mission.observations ?? []).join("\n"))}</textarea>
          </label>
        </div>
        <button class="primary" type="submit" ${state.busy ? "disabled" : ""}>Save mission update</button>
      </form>
      <div class="facts-grid">
        <div class="fact-block"><h4>Recent mission events</h4>
          ${events.length ? `<ul>${events.map((event) => `<li>${escapeHtml(event.category)} · ${escapeHtml(event.description)}</li>`).join("")}</ul>` : `<p>None recorded.</p>`}
        </div>
        <div class="fact-block"><h4>Council decisions</h4>
          ${listMarkup(mission.council_decisions)}
        </div>
      </div>
    </section>`;
}

function renderProviderPanel() {
  const rows = state.providers.map((provider) => {
    const available = provider.available && provider.model_available;
    const agent = state.agents.find((item) => item.agent_id === provider.agent_id);
    return `
      <div class="agent-card">
        <header>
          <strong>${escapeHtml(agent?.name ?? provider.agent_id)} · ${escapeHtml(provider.model)}</strong>
          <span class="badge ${available ? "good" : "bad"}">${available ? "ready" : "offline"}</span>
        </header>
        <p class="hint">${escapeHtml(provider.detail)}</p>
      </div>`;
  }).join("");
  return `
    <section class="panel">
      <div class="section-heading"><h2>AI provider status</h2></div>
      <div class="agent-list">${rows || `<div class="empty-state">Provider status has not loaded.</div>`}</div>
      <p class="hint spacer-top">Provider availability is checked through the configured local Ollama endpoint. Models may produce errors; missing analyses are never filled in automatically.</p>
    </section>`;
}

function renderMeetingHistory() {
  if (!state.meetings.length) {
    return `<div class="empty-state">No council meetings for this mission.</div>`;
  }
  return `
    <div class="history-list">
      ${state.meetings.map((meeting) => `
        <button
          class="mission-item ${meeting.meeting_id === state.selectedMeetingId ? "selected" : ""}"
          data-action="select-meeting"
          data-id="${escapeHtml(meeting.meeting_id)}"
          type="button"
        >
          <strong>${escapeHtml(meeting.motion)}</strong>
          <span>${escapeHtml(meeting.state)} · ${escapeHtml(meeting.meeting_id)}</span>
        </button>`).join("")}
    </div>`;
}

function actionButton(label, action, disabled = false, primary = false) {
  return `<button class="${primary ? "primary" : ""}" type="button" data-action="${action}" ${disabled || state.busy ? "disabled" : ""}>${label}</button>`;
}

function renderAnalysisCard(agent, analysis, discussion, phase) {
  let action = "";
  if (phase === "ANALYSIS" && !analysis) {
    action = actionButton("Run analysis", "run-analysis", false, true)
      .replace('data-action="run-analysis"', `data-action="run-analysis" data-agent="${escapeHtml(agent.agent_id)}"`);
  } else if (phase === "DISCUSSION" && analysis && !discussion) {
    const hasPeer = (state.meeting.analyses ?? []).some((item) => item.agent_id !== agent.agent_id);
    action = actionButton("Join discussion", "run-discussion", !hasPeer, true)
      .replace('data-action="run-discussion"', `data-action="run-discussion" data-agent="${escapeHtml(agent.agent_id)}"`);
  }
  return `
    <article class="agent-card">
      <header><div><strong>${escapeHtml(agent.name)}</strong><div class="hint">${escapeHtml(agent.role)}</div></div>${action}</header>
      ${analysis ? `
        <p><strong>Initial recommendation:</strong> ${escapeHtml(analysis.recommendation)}</p>
        <details><summary>Analysis details</summary>
          <p>${escapeHtml(analysis.reasoning)}</p>
          <p><strong>Risks:</strong> ${escapeHtml(analysis.main_risks)}</p>
          <p><strong>Unknowns:</strong> ${escapeHtml(analysis.missing_information)}</p>
          <p><strong>Confidence:</strong> ${Math.round(analysis.confidence * 100)}%</p>
          <p><strong>Facts:</strong> ${escapeHtml((analysis.facts ?? []).join("; ") || "None supplied")}</p>
          <p><strong>Assumptions:</strong> ${escapeHtml((analysis.assumptions ?? []).join("; ") || "None stated")}</p>
        </details>` : `<p class="hint">Analysis not yet recorded.</p>`}
      ${discussion ? `
        <details><summary>Discussion response</summary>
          <p><strong>Challenges:</strong> ${escapeHtml(discussion.challenges)}</p>
          <p><strong>Reconsideration:</strong> ${escapeHtml(discussion.reconsideration)}</p>
          <p><strong>Updated recommendation:</strong> ${escapeHtml(discussion.updated_recommendation)}</p>
          <p>${escapeHtml(discussion.reasoning)}</p>
        </details>` : ""}
    </article>`;
}

function renderVotes(meeting, agents, phase) {
  const history = meeting.vote_history ?? [];
  const latest = new Map();
  for (const vote of history) latest.set(vote.agent_id, vote);
  const rows = agents.map((agent) => {
    const vote = latest.get(agent.agent_id);
    if (phase === "INITIAL_VOTING" && !history.some(
      (item) => item.agent_id === agent.agent_id && item.revision === 0,
    )) {
      return `
        <form class="vote-form" data-form="initial-vote" data-agent="${escapeHtml(agent.agent_id)}">
          <strong>${escapeHtml(agent.name)}</strong>
          <select name="choice" aria-label="Initial vote for ${escapeHtml(agent.name)}">
            <option value="YES">YES</option><option value="NO">NO</option>
          </select>
          <button type="submit" ${state.busy ? "disabled" : ""}>Record vote</button>
        </form>`;
    }
    if (phase === "REVISION" && vote) {
      const changedChoice = vote.choice === "YES" ? "NO" : "YES";
      return `
        <form class="vote-form" data-form="vote-revision" data-agent="${escapeHtml(agent.agent_id)}">
          <strong>${escapeHtml(agent.name)} <span class="hint">Current: ${escapeHtml(vote.choice)}</span></strong>
          <select name="choice" aria-label="Revised vote for ${escapeHtml(agent.name)}">
            <option value="${changedChoice}">${changedChoice}</option>
          </select>
          <input name="explanation" maxlength="4096" required aria-label="Reason for vote revision for ${escapeHtml(agent.name)}" placeholder="Reason for changing vote">
          <button type="submit" ${state.busy ? "disabled" : ""}>Revise vote</button>
        </form>`;
    }
    return `
      <div class="history-item">
        <strong>${escapeHtml(agent.name)}</strong>
        <span class="badge ${vote ? (vote.choice === "YES" ? "good" : "bad") : ""}">${vote ? escapeHtml(vote.choice) : "not recorded"}</span>
        ${vote?.explanation ? `<p>${escapeHtml(vote.explanation)}</p>` : ""}
      </div>`;
  }).join("");
  return `<div class="stack">${rows}</div>`;
}

function renderResult(meeting) {
  const result = meeting.result;
  if (!result) return "";
  const votes = (result.votes ?? []).map((vote) => `
    <li>${escapeHtml(seatName(vote.agent_id))}: ${escapeHtml(vote.choice)}${vote.revision ? ` (revision ${vote.revision})` : ""}</li>`,
  ).join("");
  return `
    <div class="workflow-step result ${result.approved ? "" : "rejected"}">
      <div class="phase-heading">
        <div><h3>Council result · ${result.approved ? "Motion approved" : "Motion not approved"}</h3>
        <p>${result.yes_votes} YES · ${result.no_votes} NO · ${result.required_yes_votes} required</p></div>
        <span class="badge ${result.approved ? "good" : "bad"}">${result.approved ? "approved" : "not approved"}</span>
      </div>
      ${result.incomplete ? `<p class="notice">Incomplete vote: ${escapeHtml((result.missing_voters ?? []).map(seatName).join(", "))} did not record a vote. The majority result does not imply a missing vote.</p>` : ""}
      <ul>${votes}</ul>
    </div>`;
}

function renderCouncilDecision(meeting) {
  const decision = meeting.commander_decision;
  if (decision) {
    return `
      <div class="workflow-step">
        <div class="phase-heading"><div><h3>Commander decision</h3>
          <p>${escapeHtml(decision.option)}</p></div>
          <span class="badge">${escapeHtml(decision.option)}</span>
        </div>
        ${decision.explanation ? `<p>${escapeHtml(decision.explanation)}</p>` : ""}
      </div>`;
  }
  if (meeting.state !== "FINALIZED") return "";
  const options = meeting.result?.approved
    ? ["AUTHORIZE", "REJECT", "REQUEST_ANOTHER_MEETING"]
    : ["REJECT", "REQUEST_ANOTHER_MEETING"];
  return `
    <form class="workflow-step form-grid" data-form="commander-decision">
      <h3>Commander decision</h3>
      <p class="hint">Authorization requires a council majority. This application records the decision; it does not issue game commands.</p>
      <label>Decision
        <select name="option">${options.map((option) => `<option value="${option}">${option.replaceAll("_", " ")}</option>`).join("")}</select>
      </label>
      <label>Explanation <small>Required when requesting another meeting</small>
        <textarea name="explanation" maxlength="4096" placeholder="Optional explanation"></textarea>
      </label>
      <button class="primary" type="submit" ${state.busy ? "disabled" : ""}>Record decision</button>
    </form>`;
}

function renderWorkflow() {
  const meeting = state.meeting;
  if (!meeting) {
    return `<div class="empty-state">Select a meeting or create one to view its council workflow. Meetings are resumable after restarting the service.</div>`;
  }
  const analysisById = new Map((meeting.analyses ?? []).map((item) => [item.agent_id, item]));
  const discussionById = new Map((meeting.discussions ?? []).map((item) => [item.agent_id, item]));
  const agentCards = state.agents.map((agent) =>
    renderAnalysisCard(
      agent,
      analysisById.get(agent.agent_id),
      discussionById.get(agent.agent_id),
      meeting.state,
    ),
  ).join("");
  let phaseBody = "";
  if (meeting.state === "MEETING_STARTED") {
    phaseBody = `<p class="hint">All council members will receive the same saved mission snapshot.</p>${actionButton("Start independent analysis", "start-analysis", false, true)}`;
  } else if (meeting.state === "ANALYSIS" || meeting.state === "DISCUSSION") {
    phaseBody = `<div class="agent-list">${agentCards}</div>`;
    if (meeting.state === "ANALYSIS") {
      const analysisCount = meeting.analyses?.length ?? 0;
      phaseBody += `<p class="hint">At least two recorded analyses are needed for meaningful peer discussion (${analysisCount}/${state.agents.length}).</p>`;
      phaseBody += actionButton(
        "Start discussion",
        "start-discussion",
        analysisCount < 2,
        true,
      );
    } else {
      const discussionCount = meeting.discussions?.length ?? 0;
      phaseBody += `<p class="hint">${discussionCount} discussion response(s) recorded. Continue when ready.</p>`;
      phaseBody += actionButton(
        "Start manual voting",
        "start-voting",
        discussionCount < 1,
        true,
      );
    }
  } else if (meeting.state === "INITIAL_VOTING") {
    phaseBody = `
      <p class="notice">Votes are entered manually by the operator; the AI provides analysis and discussion, not automatic voting.</p>
      ${renderVotes(meeting, state.agents, meeting.state)}
      ${actionButton("Close initial voting and begin revision", "start-revision", false, true)}`;
  } else if (meeting.state === "REVISION") {
    phaseBody = `
      <p class="notice">A changed vote requires a recorded explanation. Missing votes remain missing.</p>
      ${renderVotes(meeting, state.agents, meeting.state)}
      ${actionButton("Finalize council result", "finalize", false, true)}`;
  } else {
    phaseBody = renderResult(meeting) + renderCouncilDecision(meeting);
  }
  return `
    <section class="workflow">
      <div class="workflow-step">
        <div class="phase-heading">
          <div><p class="section-kicker">Canonical motion</p><h3>${escapeHtml(meeting.motion)}</h3></div>
          <span class="badge">${escapeHtml(meeting.state)}</span>
        </div>
        <p class="hint">Meeting ${escapeHtml(meeting.meeting_id)} · Mission snapshot: ${escapeHtml(meeting.mission_snapshot?.name ?? "saved")}</p>
      </div>
      <div class="workflow-step">
        <div class="phase-heading"><div><h3>${escapeHtml(meeting.state.replaceAll("_", " "))}</h3>
        <p>Each transition is saved to local SQLite history.</p></div></div>
        ${phaseBody}
      </div>
    </section>`;
}

function renderMeetingsPanel() {
  if (!state.mission) return "";
  return `
    <section class="panel">
      <div class="section-heading">
        <div><p class="section-kicker">Council</p><h2>Meetings</h2></div>
        <span class="badge">${state.meetings.length}</span>
      </div>
      <form class="field-grid" data-form="create-meeting">
        <label class="wide">Canonical motion
          <input name="motion" maxlength="4096" required placeholder="State the exact proposal for council review">
        </label>
        <button class="primary" type="submit" ${state.busy ? "disabled" : ""}>Create council meeting</button>
      </form>
      <div class="spacer-top">${renderMeetingHistory()}</div>
      ${state.meeting ? `<div class="spacer-top">${renderWorkflow()}</div>` : ""}
    </section>`;
}

function render() {
  const connection = state.socketConnected
    ? `<span class="pill connected">Live updates connected</span>`
    : `<span class="pill">Live updates reconnecting</span>`;
  app.innerHTML = `
    <div class="shell">
      <header class="topbar">
        <div class="brand">
          <div class="brand-mark" aria-hidden="true">AI</div>
          <div><h1>Operation AI Squad</h1><p>Local mission planning console</p></div>
        </div>
        <div class="topbar-status">
          <span class="pill simulation">Simulation only · no game connection</span>
          ${connection}
          ${renderProviderStatus()}
        </div>
      </header>
      ${state.error ? `<div class="notice error spacer-top" role="alert">${escapeHtml(state.error)}</div>` : ""}
      <div class="layout">
        ${renderMissionSidebar()}
        <main class="main-column">
          <div class="content-grid">
            ${renderMissionPanel()}
            ${renderProviderPanel()}
          </div>
          ${renderMeetingsPanel()}
          <div class="notice">Door Kickers 2 is not connected. Mission facts are operator-entered; this dashboard cannot read game state or issue in-game actions.</div>
        </main>
      </div>
    </div>`;
}

function parseLines(value) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function parseOperators(value) {
  return parseLines(value).map((line) => {
    const [operator_id, status = "", assignment = "", available = ""] = line
      .split("|").map((part) => part.trim());
    if (!operator_id) throw new Error("Every operator needs an ID.");
    if (available && !["true", "false"].includes(available.toLowerCase())) {
      throw new Error(`Availability for ${operator_id} must be true, false, or blank.`);
    }
    return {
      operator_id,
      status: status || null,
      assignment: assignment || null,
      available: available ? available.toLowerCase() === "true" : null,
    };
  });
}

async function perform(task) {
  state.error = "";
  state.busy = true;
  render();
  let failure = null;
  try {
    await task();
  } catch (error) {
    failure = error;
  }
  try {
    await loadWorkspace();
  } catch (error) {
    failure ??= error;
  }
  state.error = failure
    ? failure instanceof Error ? failure.message : String(failure)
    : "";
  state.busy = false;
  render();
}

async function selectMission(missionId) {
  state.selectedMissionId = missionId;
  state.selectedMeetingId = null;
  state.error = "";
  try {
    await loadWorkspace();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  }
  render();
}

async function handleSubmit(form) {
  const data = new FormData(form);
  const value = (name) => String(data.get(name) ?? "");
  const json = (object) => JSON.stringify(object);
  switch (form.dataset.form) {
    case "create-mission": {
      const mission = await api("/api/missions", {
        method: "POST",
        body: json({
          mission_id: value("mission_id"),
          name: value("name"),
          objective: value("objective"),
        }),
      });
      state.selectedMissionId = mission.mission_id;
      state.selectedMeetingId = null;
      break;
    }
    case "update-mission": {
      const payload = {
        objective: value("objective"),
        operators: parseOperators(value("operators")),
        available_equipment: parseLines(value("available_equipment")),
        known_threats: parseLines(value("known_threats")),
        unknown_information: parseLines(value("unknown_information")),
        observations: parseLines(value("observations")),
      };
      const status = value("status");
      if (status !== state.mission.status) payload.status = status;
      await api(`/api/missions/${encodeURIComponent(state.selectedMissionId)}`, {
        method: "PATCH",
        body: json(payload),
      });
      break;
    }
    case "create-meeting": {
      const meeting = await api(
        `/api/missions/${encodeURIComponent(state.selectedMissionId)}/meetings`,
        { method: "POST", body: json({ motion: value("motion") }) },
      );
      state.selectedMeetingId = meeting.meeting_id;
      break;
    }
    case "initial-vote":
      await api(`/api/meetings/${encodeURIComponent(state.selectedMeetingId)}/votes`, {
        method: "POST",
        body: json({ agent_id: form.dataset.agent, choice: value("choice") }),
      });
      break;
    case "vote-revision":
      await api(
        `/api/meetings/${encodeURIComponent(state.selectedMeetingId)}/votes/${encodeURIComponent(form.dataset.agent)}/revision`,
        {
          method: "POST",
          body: json({ choice: value("choice"), explanation: value("explanation") }),
        },
      );
      break;
    case "commander-decision":
      await api(
        `/api/meetings/${encodeURIComponent(state.selectedMeetingId)}/commander-decision`,
        {
          method: "POST",
          body: json({ option: value("option"), explanation: value("explanation") }),
        },
      );
      break;
    default:
      throw new Error("Unknown dashboard form.");
  }
}

async function handleAction(button) {
  const action = button.dataset.action;
  const meetingId = encodeURIComponent(state.selectedMeetingId ?? "");
  switch (action) {
    case "select-mission":
      await selectMission(button.dataset.id);
      return;
    case "select-meeting":
      state.selectedMeetingId = button.dataset.id;
      await perform(async () => {});
      return;
    case "run-analysis":
      await perform(() => api(`/api/meetings/${meetingId}/analysis`, {
        method: "POST",
        body: JSON.stringify({ agent_id: button.dataset.agent }),
      }));
      return;
    case "run-discussion":
      await perform(() => api(`/api/meetings/${meetingId}/discussion`, {
        method: "POST",
        body: JSON.stringify({ agent_id: button.dataset.agent }),
      }));
      return;
    case "start-analysis":
      await perform(() => api(`/api/meetings/${meetingId}/analysis/start`, { method: "POST" }));
      return;
    case "start-discussion":
      await perform(() => api(`/api/meetings/${meetingId}/discussion/start`, { method: "POST" }));
      return;
    case "start-voting":
      await perform(() => api(`/api/meetings/${meetingId}/votes/start`, { method: "POST" }));
      return;
    case "start-revision":
      await perform(() => api(`/api/meetings/${meetingId}/revision/start`, { method: "POST" }));
      return;
    case "finalize":
      await perform(() => api(`/api/meetings/${meetingId}/finalize`, { method: "POST" }));
      return;
    default:
      state.error = "Unknown dashboard action.";
      render();
  }
}

app.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  void perform(() => handleSubmit(form));
});

app.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  void handleAction(button);
});

function connectEvents() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/events`);
  socket.addEventListener("open", () => {
    state.socketConnected = true;
    render();
    socket.send("ping");
  });
  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      state.error = "The live event stream returned invalid data.";
      render();
      return;
    }
    if (message.event === "pong" || message.event === "connected") return;
    if (state.busy) return;
    void loadWorkspace().then(render).catch((error) => {
      state.error = error instanceof Error ? error.message : String(error);
      render();
    });
  });
  socket.addEventListener("close", () => {
    state.socketConnected = false;
    render();
    window.setTimeout(connectEvents, 2500);
  });
  socket.addEventListener("error", () => socket.close());
}

async function start() {
  try {
    await loadWorkspace();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  }
  render();
  connectEvents();
}

void start();
