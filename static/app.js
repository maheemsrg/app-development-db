// Design Studio — front-end logic.
// Loads available models into the top-right dropdown, then runs a live chat
// against the selected model.

const chatEl = document.getElementById("chat");
const welcomeEl = document.getElementById("welcome");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const buildDeployEl = document.getElementById("buildDeploy");
const modelSelect = document.getElementById("modelSelect");
const hintEl = document.getElementById("hint");
const newChatBtn = document.getElementById("newChat");
const convTitle = document.getElementById("convTitle");
const buildStatusEl = document.getElementById("buildStatus");
const buildResultEl = document.getElementById("buildResult");
const promoteBtnEl = document.getElementById("promoteBtn");
const promoteModalEl = document.getElementById("promoteModal");
const promoteCloseEl = document.getElementById("promoteClose");
const promoteGenerateBtn = document.getElementById("promoteGenerate");
const promoteResultsEl = document.getElementById("promoteResults");
const ideateBtnEl = document.getElementById("ideateBtn");
const ideaCardsEl = document.getElementById("ideaCards");
const buildPromptCardEl = document.getElementById("buildPromptCard");

let messages = []; // {role, content}
let busy = false;
let selectedIdea = null;
let lastBuildCtx = null; // { messages, workspace_path, app_name }
let modelCatalog = []; // {id, name, chat, task}
const BUILD_INTENT_PATTERNS = [
  /build\s*(and|&)\s*deploy/i,
  /create\s+app/i,
  /build\s+app/i,
  /deploy\s+(this|the)\s+app/i,
  /deploy\s+app/i,
];

// ── Conversation persistence ──────────────────────────────────────────────────
const STORAGE_KEY = 'ds_conversations';
const MAX_STORED = 20;

function genId() { return 'ds_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7); }

let currentConvId = null;

function allConversations() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); } catch { return []; }
}

function saveConversations(list) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(0, MAX_STORED))); } catch {}
}

function saveCurrentConversation() {
  if (!currentConvId || messages.length === 0) return;
  const title = convTitle.textContent !== 'New design' ? convTitle.textContent : messages[0]?.content?.slice(0, 48) || 'New design';
  const list = allConversations().filter(c => c.id !== currentConvId);
  list.unshift({ id: currentConvId, title, messages: [...messages], updatedAt: Date.now() });
  saveConversations(list);
  renderRecentList();
}

function renderRecentList() {
  const list = allConversations();
  const recentEl = document.getElementById('recent');
  if (!recentEl) return;
  if (list.length === 0) {
    recentEl.innerHTML = '<div class="recent-empty">No conversations yet</div>';
    return;
  }
  recentEl.innerHTML = list.map(c => `
    <button class="recent-item${c.id === currentConvId ? ' active' : ''}" data-id="${c.id}" title="${escapeHtml(c.title)}">
      ${escapeHtml(c.title.slice(0, 40))}
    </button>
  `).join('');
  recentEl.querySelectorAll('.recent-item').forEach(btn => {
    btn.addEventListener('click', () => restoreConversation(btn.dataset.id));
  });
}

function restoreConversation(id) {
  const conv = allConversations().find(c => c.id === id);
  if (!conv) return;
  currentConvId = id;
  messages = [...conv.messages];
  convTitle.textContent = conv.title;
  chatEl.innerHTML = '';
  chatEl.appendChild(welcomeEl);
  welcomeEl.style.display = 'none';
  messages.forEach(m => addMessage(m.role, m.content));
  clearBuildStatus();
  renderRecentList();
}

function startNewConversation() {
  currentConvId = genId();
  messages = [];
  chatEl.innerHTML = '';
  chatEl.appendChild(welcomeEl);
  welcomeEl.style.display = '';
  convTitle.textContent = 'New design';
  clearBuildStatus();
  renderRecentList();
}

// ---------- Model dropdown ----------
async function loadModels() {
  try {
    const res = await fetch("/api/models");
    const data = await res.json();
    const diag = data.diagnostics || {};
    const previous = modelSelect.value;
    const rawModels = Array.isArray(data.models) ? data.models : [];
    modelCatalog = rawModels
      .map((m) => normalizeModel(m))
      .filter((m) => m && m.id);

    modelSelect.innerHTML = "";
    if (modelCatalog.length > 0) {
      modelCatalog.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.chat ? prettyName(m.name) : `${prettyName(m.name)} (non-chat)`;
        opt.disabled = !m.chat;
        opt.dataset.chat = String(Boolean(m.chat));
        modelSelect.appendChild(opt);
      });

      const chatModels = modelCatalog.filter((m) => m.chat);
      if (
        previous &&
        modelCatalog.some((m) => m.id === previous && m.chat)
      ) {
        modelSelect.value = previous;
      } else if (chatModels.length > 0) {
        modelSelect.value = chatModels[0].id;
      }

      if (chatModels.length === 0) {
        hintEl.textContent = "No allowlisted chat models available";
      } else if (chatModels.length !== modelCatalog.length) {
        hintEl.textContent = `Ready (${chatModels.length} curated chat models)`;
      } else {
        hintEl.textContent =
          data.source === "fallback"
            ? `Showing a default model list (live lookup unavailable${fallbackReason(diag)})`
            : `Ready (${chatModels.length} curated chat models)`;
      }

    } else {
      modelSelect.innerHTML = "<option>No accessible models</option>";
      hintEl.textContent = "No serving endpoints available for this user";
    }
  } catch (e) {
    modelSelect.innerHTML = "<option>Could not load models</option>";
    hintEl.textContent = "Could not load models";
  }
}

function fallbackReason(diag) {
  const status = diag.http_status;
  const path = diag.endpoint_path;
  if (status && path) return `: ${status} on ${path}`;
  if (status) return `: HTTP ${status}`;
  return "";
}

function prettyName(name) {
  return name
    .replace(/^(databricks-|models\/|serving-endpoints\/)/, "")
    .replace(/^system\.ai\./, "")
    .replace(/[-_/]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeModel(model) {
  if (typeof model === "string") {
    return { id: model, name: model, chat: true, task: null };
  }
  if (!model || typeof model !== "object") {
    return null;
  }
  const id = model.id || model.name;
  if (!id) return null;
  return {
    id,
    name: model.name || id,
    chat: model.chat !== false,
    task: model.task || null,
  };
}

function parseErrorDetail(detail, status) {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    if (typeof detail.message === "string") return detail.message;
    return `Error ${status}`;
  }
  return `Error ${status}`;
}

function setBuildStatus(text, state = "running") {
  buildStatusEl.hidden = false;
  buildStatusEl.className = `build-status ${state}`;
  buildStatusEl.textContent = text;
}

function formatStepMessage(step) {
  let msg = step.step;
  if (step.model) msg += ` (${prettyName(step.model)})`;
  if (step.file_count != null) msg += ` — ${step.file_count} file${step.file_count !== 1 ? "s" : ""}`;
  if (step.workspace_path) msg += ` → ${step.workspace_path}`;
  if (step.app_name && step.step !== "Done") msg += `: ${step.app_name}`;
  if (step.app_url) msg += ` (${step.app_url})`;
  return msg;
}

function renderBuildSteps(steps, jobStatus) {
  if (!steps || steps.length === 0) return;
  const allStepsHtml = steps.map(s =>
    `<div class="build-step">${escapeHtml(formatStepMessage(s))}</div>`
  ).join("");
  buildStatusEl.innerHTML = allStepsHtml;
  buildStatusEl.hidden = false;
  buildStatusEl.className = `build-status ${jobStatus === "failed" ? "error" : jobStatus === "completed" ? "done" : "running"}`;
}

function looksLikeBuildIntent(text) {
  return BUILD_INTENT_PATTERNS.some((pattern) => pattern.test(text || ""));
}

function clearBuildStatus() {
  buildStatusEl.hidden = true;
  buildStatusEl.textContent = "";
  buildStatusEl.className = "build-status";
  buildResultEl.hidden = true;
  buildResultEl.innerHTML = "";
}

function renderFilePreview(files) {
  const tabs = files
    .map((f, i) =>
      `<button class="file-tab${i === 0 ? " active" : ""}" data-idx="${i}">${escapeHtml(f.path)}</button>`
    )
    .join("");
  const firstContent = files.length > 0 ? `<pre><code>${escapeHtml(files[0].content)}</code></pre>` : "";
  return `
    <details class="file-preview">
      <summary>View generated files (${files.length} file${files.length !== 1 ? "s" : ""})</summary>
      <div class="file-tabs">
        <div class="file-tab-bar">${tabs}</div>
        <div class="file-tab-content">${firstContent}</div>
      </div>
    </details>
  `;
}

function renderBuildResult(result) {
  const requestedWorkspace = result.requested_workspace_path || result.workspace_source_path || "N/A";
  const effectiveWorkspace = result.effective_workspace_path || result.workspace_source_path || "N/A";
  const workspaceMode = result.workspace_path_mode || "unknown";
  const fallbackReason = result.workspace_path_fallback_reason;
  const appUrl = result.app_url;
  const appName = result.app_name || "N/A";
  const deploymentStatus = result.deployment_status || "unknown";
  const genFiles = Array.isArray(result.generated_files) ? result.generated_files : [];

  buildResultEl.hidden = false;
  buildResultEl.innerHTML = `
    <div><strong>App:</strong> ${escapeHtml(appName)}</div>
    <div><strong>Requested workspace path:</strong> <a href="${escapeHtml(requestedWorkspace)}" target="_blank" rel="noopener noreferrer">${escapeHtml(requestedWorkspace)}</a></div>
    <div><strong>Effective workspace path:</strong> <a href="${escapeHtml(effectiveWorkspace)}" target="_blank" rel="noopener noreferrer">${escapeHtml(effectiveWorkspace)}</a></div>
    <div><strong>Workspace path mode:</strong> ${escapeHtml(String(workspaceMode))}</div>
    ${fallbackReason ? `<div><strong>Workspace fallback reason:</strong> ${escapeHtml(String(fallbackReason))}</div>` : ""}
    <div><strong>App URL:</strong> ${appUrl ? `<a href="${escapeHtml(appUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(appUrl)}</a>` : "N/A"}</div>
    <div><strong>Deployment:</strong> ${escapeHtml(String(deploymentStatus))}</div>
    ${genFiles.length > 0 ? renderFilePreview(genFiles) : ""}
  `;

  if (genFiles.length > 0) {
    const tabBar = buildResultEl.querySelector(".file-tab-bar");
    const contentEl = buildResultEl.querySelector(".file-tab-content");
    if (tabBar && contentEl) {
      tabBar.addEventListener("click", (e) => {
        const btn = e.target.closest(".file-tab");
        if (!btn) return;
        tabBar.querySelectorAll(".file-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const idx = parseInt(btn.dataset.idx, 10);
        contentEl.innerHTML = `<pre><code>${escapeHtml(genFiles[idx].content)}</code></pre>`;
      });
    }
  }
}

async function pollBuildJob(jobId) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const res = await fetch(`/api/build-and-deploy/${encodeURIComponent(jobId)}`);
    if (!res.ok) {
      throw new Error("Could not read deployment job status");
    }
    const data = await res.json();
    const steps = Array.isArray(data.steps) ? data.steps : [];
    renderBuildSteps(steps, data.status);
    if (data.status === "completed") {
      if (data.result) {
        renderBuildResult(data.result);
        lastBuildCtx = {
          messages: [...messages],
          workspace_path: data.result.effective_workspace_path || data.result.workspace_source_path,
          app_name: data.result.app_name,
        };
        promoteBtnEl.disabled = false;
      }
      return data;
    }
    if (data.status === "failed") {
      const detail = data.error?.detail || data.error?.message || "Build and deploy failed.";
      throw new Error(detail);
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
  throw new Error("Build timed out while waiting for deployment status.");
}

async function startBuildForPrompt(text) {
  if (!text || busy) return;
  const model = modelSelect.value;
  if (!model || model.startsWith("Loading") || model.startsWith("Could")) {
    hintEl.textContent = "Pick a model first";
    return;
  }

  busy = true;
  sendEl.disabled = true;
  ideateBtnEl.disabled = true;
  buildDeployEl.disabled = true;
  setBuildStatus("Databricks workspace build: Generating code", "running");
  buildResultEl.hidden = true;

  try {
    const explicitName = convTitle.textContent !== "New design" ? convTitle.textContent : null;
    const start = await fetch("/api/build-and-deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_request: text,
        model,
        ...(explicitName ? { project_name: explicitName } : {}),
      }),
    });
    if (!start.ok) {
      const err = await start.json().catch(() => ({}));
      throw new Error(parseErrorDetail(err.detail, start.status));
    }
    const startData = await start.json();
    addMessage("assistant", "Build & Deploy is running in your Databricks workspace.");
    await pollBuildJob(startData.job_id);
    hintEl.textContent = "Databricks build and deploy completed";
  } catch (e) {
    setBuildStatus(`Databricks workspace build: Failed - ${e.message}`, "error");
    hintEl.textContent = "Build and deploy failed";
  } finally {
    busy = false;
    sendEl.disabled = false;
    ideateBtnEl.disabled = false;
    buildDeployEl.disabled = false;
    inputEl.focus();
  }
}

async function buildAndDeploy() {
  const text = inputEl.value.trim();
  if (!text) return;
  messages.push({ role: "user", content: text });
  addMessage("user", text);
  inputEl.value = "";
  autoGrow();
  await startBuildForPrompt(text);
}

// ---------- Ideate ----------
async function ideate() {
  if (busy) return;
  const text = inputEl.value.trim();
  if (!text) {
    hintEl.textContent = "Describe your problem or job first";
    return;
  }
  const model = modelSelect.value;
  if (!model || model.startsWith("Loading") || model.startsWith("Could")) {
    hintEl.textContent = "Pick a model first";
    return;
  }

  busy = true;
  sendEl.disabled = true;
  buildDeployEl.disabled = true;
  ideateBtnEl.disabled = true;

  ideaCardsEl.innerHTML = '<div class="idea-loading">Generating ideas…</div>';
  ideaCardsEl.hidden = false;
  buildPromptCardEl.hidden = true;
  buildPromptCardEl.innerHTML = "";

  addMessage("user", text);
  messages.push({ role: "user", content: text });
  inputEl.value = "";
  autoGrow();

  try {
    const res = await fetch("/api/ideate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: text, model }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(parseErrorDetail(err.detail, res.status));
    }
    const data = await res.json();
    const ideas = data.ideas || [];

    const cardsHtml = ideas.map((idea, idx) => `
      <div class="idea-card" data-idx="${idx}">
        <div class="idea-title">${escapeHtml(idea.title || "")}</div>
        <div class="idea-desc">${escapeHtml(idea.description || "")}</div>
        <div class="idea-why">\u{1F4A1} Why this works: ${escapeHtml(idea.why || "")}</div>
      </div>
    `).join("");

    ideaCardsEl.innerHTML = `
      <div class="idea-cards-header">Here are 5 ideas based on your input:</div>
      <div class="idea-grid">${cardsHtml}</div>
      <div class="idea-actions" id="ideaActions" hidden>
        <span id="ideaSelectedName" class="idea-selected-name"></span>
        <button id="knowMoreBtn" class="idea-action-btn">Know more</button>
        <button id="buildIdeaBtn" class="idea-action-btn primary">Build it</button>
      </div>
    `;

    // Wire up card clicks
    ideaCardsEl.querySelectorAll(".idea-card").forEach(card => {
      card.addEventListener("click", () => {
        ideaCardsEl.querySelectorAll(".idea-card").forEach(c => c.classList.remove("selected"));
        card.classList.add("selected");
        const idx = parseInt(card.dataset.idx, 10);
        selectedIdea = ideas[idx];
        document.getElementById("ideaActions").hidden = false;
        document.getElementById("ideaSelectedName").textContent = selectedIdea.title;
      });
    });

    // Wire up "Know more"
    document.getElementById("knowMoreBtn").addEventListener("click", () => {
      if (!selectedIdea) return;
      ideaCardsEl.hidden = true;
      buildPromptCardEl.hidden = true;
      inputEl.value = `Tell me more about: ${selectedIdea.title} — ${selectedIdea.description}`;
      autoGrow();
      send();
    });

    // Wire up "Build it"
    document.getElementById("buildIdeaBtn").addEventListener("click", async () => {
      if (!selectedIdea) return;
      const buildBtn = document.getElementById("buildIdeaBtn");
      buildBtn.disabled = true;
      buildPromptCardEl.innerHTML = '<div class="idea-loading">Generating build prompt…</div>';
      buildPromptCardEl.hidden = false;

      try {
        const pRes = await fetch("/api/ideate/prompt", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            idea_title: selectedIdea.title,
            idea_description: selectedIdea.description,
            messages,
            model,
          }),
        });
        if (!pRes.ok) {
          const err = await pRes.json().catch(() => ({}));
          throw new Error(parseErrorDetail(err.detail, pRes.status));
        }
        const pData = await pRes.json();
        const promptText = pData.prompt || "";

        buildPromptCardEl.innerHTML = `
          <div class="build-prompt-header">Generated build prompt</div>
          <div class="build-prompt-text">${escapeHtml(promptText)}</div>
          <button id="runBuildBtn" class="promote-generate-btn">Build &amp; Deploy &#x2197;</button>
        `;

        document.getElementById("runBuildBtn").addEventListener("click", () => {
          ideaCardsEl.hidden = true;
          buildPromptCardEl.hidden = true;
          startBuildForPrompt(promptText);
        });
      } catch (e) {
        buildPromptCardEl.innerHTML = `<div class="idea-loading" style="color:#f85149">Error: ${escapeHtml(e.message)}</div>`;
        buildBtn.disabled = false;
      }
    });

    hintEl.textContent = "Select an idea to continue";
  } catch (e) {
    ideaCardsEl.innerHTML = `<div class="idea-loading" style="color:#f85149">Error: ${escapeHtml(e.message)}</div>`;
    hintEl.textContent = "Ideation failed";
  } finally {
    busy = false;
    sendEl.disabled = false;
    buildDeployEl.disabled = false;
    ideateBtnEl.disabled = false;
    inputEl.focus();
  }
}

ideateBtnEl.addEventListener("click", ideate);


// ---------- Rendering ----------
function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Minimal markdown: fenced code blocks + inline code + paragraphs.
function renderMarkdown(text) {
  const parts = text.split(/```/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[a-zA-Z0-9]*\n/, "");
      html += `<pre><code>${escapeHtml(body)}</code></pre>`;
    } else {
      const esc = escapeHtml(part).replace(/`([^`]+)`/g, "<code>$1</code>");
      esc.split(/\n{2,}/).forEach((para) => {
        if (para.trim()) html += `<p>${para.replace(/\n/g, "<br>")}</p>`;
      });
    }
  });
  return html;
}

function addMessage(role, content, opts = {}) {
  if (welcomeEl) welcomeEl.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}` + (opts.thinking ? " thinking" : "");
  const roleLabel = role === "user" ? "You" : "AI";
  wrap.innerHTML = `<div class="role">${roleLabel}</div><div class="body"></div>`;
  const body = wrap.querySelector(".body");
  if (role === "assistant" && !opts.thinking) {
    body.innerHTML = renderMarkdown(content);
  } else {
    body.textContent = content;
  }
  chatEl.appendChild(wrap);
  chatEl.scrollTop = chatEl.scrollHeight;
  return wrap;
}

// ---------- Sending ----------
async function send() {
  const text = inputEl.value.trim();
  if (!text || busy) return;
  const model = modelSelect.value;
  if (!model || model.startsWith("Loading") || model.startsWith("Could")) {
    hintEl.textContent = "Pick a model first";
    return;
  }
  const selected = modelCatalog.find((m) => m.id === model);
  if (selected && !selected.chat) {
    hintEl.textContent = "Selected model is not chat-capable";
    return;
  }

  if (looksLikeBuildIntent(text)) {
    messages.push({ role: "user", content: text });
    addMessage("user", text);
    if (convTitle.textContent === "New design") {
      convTitle.textContent = text.slice(0, 48);
    }
    inputEl.value = "";
    autoGrow();
    hintEl.textContent = "Routing request to Databricks Build & Deploy";
    await startBuildForPrompt(text);
    return;
  }

  busy = true;
  sendEl.disabled = true;
  ideateBtnEl.disabled = true;
  inputEl.value = "";
  autoGrow();

  messages.push({ role: "user", content: text });
  addMessage("user", text);
  if (convTitle.textContent === "New design") {
    convTitle.textContent = text.slice(0, 48);
  }

  const thinking = addMessage("assistant", "Thinking…", { thinking: true });
  hintEl.textContent = `Asking ${prettyName(model)}…`;

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(parseErrorDetail(err.detail, res.status));
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let accumulated = "";
    let assistantWrap = null;
    let assistantBody = null;

    thinking.remove();

    outer: while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const ssePayload = line.slice(6).trim();
        if (ssePayload === "[DONE]") break outer;
        try {
          const obj = JSON.parse(ssePayload);
          if (obj.error) {
            if (!assistantWrap) addMessage("assistant", "⚠️ " + obj.error);
            else assistantBody.textContent = "⚠️ " + obj.error;
            hintEl.textContent = "Something went wrong";
            busy = false;
            sendEl.disabled = false;
            inputEl.focus();
            return;
          }
          if (obj.delta) {
            accumulated += obj.delta;
            if (!assistantWrap) {
              assistantWrap = document.createElement("div");
              assistantWrap.className = "msg assistant";
              assistantWrap.innerHTML = '<div class="role">AI</div><div class="body"></div>';
              assistantBody = assistantWrap.querySelector(".body");
              chatEl.appendChild(assistantWrap);
            }
            assistantBody.innerHTML = renderMarkdown(accumulated);
            chatEl.scrollTop = chatEl.scrollHeight;
          }
        } catch (_) {}
      }
    }

    if (accumulated) {
      messages.push({ role: "assistant", content: accumulated });
      saveCurrentConversation();
    } else if (!assistantWrap) {
      addMessage("assistant", "(no response)");
    }
    hintEl.textContent = "Ready";
  } catch (e) {
    thinking.remove();
    addMessage("assistant", "⚠️ " + e.message);
    hintEl.textContent = "Something went wrong";
  } finally {
    busy = false;
    sendEl.disabled = false;
    ideateBtnEl.disabled = false;
    inputEl.focus();
  }
}

// ---------- Promote ----------
const DOC_LABELS = {
  architecture: "Architecture",
  security: "Security",
  jira_stories: "Jira Stories",
  test_cases: "Test Cases",
  build_prompt: "Build Prompt",
};

function openPromoteModal() {
  promoteResultsEl.hidden = true;
  promoteResultsEl.innerHTML = "";
  promoteGenerateBtn.disabled = false;
  promoteModalEl.hidden = false;
}

async function generatePromoteDocs() {
  const selected = [...document.querySelectorAll(".promote-checks input:checked")].map((cb) => cb.value);
  if (!selected.length) {
    promoteResultsEl.hidden = false;
    promoteResultsEl.innerHTML = '<div class="modal-error">Select at least one document type.</div>';
    return;
  }
  promoteGenerateBtn.disabled = true;
  promoteResultsEl.hidden = false;
  promoteResultsEl.innerHTML = '<div class="modal-loading">Generating documents — this may take a minute…</div>';

  const model = modelSelect.value;
  const ctx = lastBuildCtx || {};

  try {
    const res = await fetch("/api/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        messages: ctx.messages || messages,
        selected_docs: selected,
        workspace_path: ctx.workspace_path || null,
      }),
    });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail || `Server returned ${res.status}`);
    }
    const data = await res.json();

    let html = "";
    for (const [key, content] of Object.entries(data.documents || {})) {
      const label = DOC_LABELS[key] || key;
      const body = typeof content === "string"
        ? renderMarkdown(content)
        : `<div class="modal-error">${escapeHtml(content.error || "Error")}</div>`;
      html += `<details class="promote-doc" open><summary>${label}</summary><div class="promote-doc-body">${body}</div></details>`;
    }
    if (data.uploaded_to) {
      html += `<div class="promote-upload-note">Saved to workspace: <code>${escapeHtml(data.uploaded_to)}</code></div>`;
    }
    promoteResultsEl.innerHTML = html || '<div class="modal-error">No documents generated.</div>';
  } catch (e) {
    promoteResultsEl.innerHTML = `<div class="modal-error">Failed: ${escapeHtml(e.message)}</div>`;
  } finally {
    promoteGenerateBtn.disabled = false;
  }
}

promoteGenerateBtn.addEventListener("click", generatePromoteDocs);
promoteCloseEl.addEventListener("click", () => { promoteModalEl.hidden = true; });
promoteModalEl.addEventListener("click", (e) => {
  if (e.target === promoteModalEl) promoteModalEl.hidden = true;
});
promoteBtnEl.addEventListener("click", openPromoteModal);

// ---------- Input behaviour ----------
function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + "px";
}

inputEl.addEventListener("input", autoGrow);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
sendEl.addEventListener("click", send);
buildDeployEl.addEventListener("click", buildAndDeploy);

newChatBtn.addEventListener("click", () => {
  startNewConversation();
  inputEl.focus();
});

loadModels();
inputEl.focus();

// Restore or start conversation
const stored = allConversations();
if (stored.length > 0) {
  currentConvId = stored[0].id;
  // Don't auto-restore messages — just show history in sidebar
} else {
  currentConvId = genId();
}
renderRecentList();
