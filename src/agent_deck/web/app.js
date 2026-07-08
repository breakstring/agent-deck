/*
 * Agent Deck N4 Pro 配置 GUI 交互。
 * 边界：从本地 daemon 读取/保存 10 键布局，只做预览和 runtime 配置，不直接访问硬件或执行动作。
 */

let appChoices = [
  { name: "Terminal", token: "T", path: "/System/Applications/Utilities/Terminal.app", color: "linear-gradient(135deg, #2f3540, #0c0f13)" },
  { name: "Chrome", token: "C", path: "/Applications/Google Chrome.app", color: "linear-gradient(135deg, #55a6ff, #1f5fc7)" },
  { name: "Cursor", token: "Cu", path: "/Applications/Cursor.app", color: "linear-gradient(135deg, #f0f3f6, #636d78)", darkText: true },
  { name: "Ghostty", token: "G", path: "/Applications/Ghostty.app", color: "linear-gradient(135deg, #8057ff, #32196d)" },
  { name: "Finder", token: "F", path: "/System/Library/CoreServices/Finder.app", color: "linear-gradient(135deg, #7dccff, #2c6ecb)" },
];

const fallbackKeys = Array.from({ length: 10 }, (_, index) => {
  if (index < 5) {
    return { index, role: "quick", kind: "unassigned", dirty: false };
  }
  return { index, role: "agent", kind: "agent", slot: index - 4, dirty: false };
});

const state = {
  keys: fallbackKeys,
  selectedIndex: 0,
  appQuery: "",
  dirty: false,
  status: null,
  saving: false,
  refreshingApps: false,
  awaitingHardwareApply: false,
  lastSaveStartedAt: null,
  keyLayoutSource: "default",
};

const el = {
  keyGrid: document.getElementById("keyGrid"),
  selectedEyebrow: document.getElementById("selectedEyebrow"),
  selectedTitle: document.getElementById("selectedTitle"),
  selectedSubtitle: document.getElementById("selectedSubtitle"),
  inspectorBody: document.getElementById("inspectorBody"),
  saveButton: document.getElementById("saveButton"),
  syncState: document.getElementById("syncState"),
  deviceDot: document.getElementById("deviceDot"),
  deviceState: document.getElementById("deviceState"),
  rendererState: document.getElementById("rendererState"),
  agentState: document.getElementById("agentState"),
  panelState: document.getElementById("panelState"),
  panelKind: document.getElementById("panelKind"),
  panelHint: document.getElementById("panelHint"),
  toast: document.getElementById("toast"),
  appModal: document.getElementById("appModal"),
  appSearch: document.getElementById("appSearch"),
  refreshApps: document.getElementById("refreshApps"),
  appCount: document.getElementById("appCount"),
  appList: document.getElementById("appList"),
  closeAppModal: document.getElementById("closeAppModal"),
};

function keyLabel(key) {
  if (key.kind === "unassigned") return "未设置快捷动作";
  if (key.kind === "app") return "打开或切换 App";
  if (key.kind === "url") return "打开网址";
  if (key.kind === "folder") return "打开文件夹";
  if (key.kind === "agent") return "Agent 状态槽位";
  if (key.kind === "disabled") return "已关闭";
  return "按键";
}

function runtimeAgents() {
  return state.status?.agents || [];
}

function agentForSlot(slot) {
  return runtimeAgents()[slot - 1] || null;
}

function agentVisualClass(agent) {
  if (!agent) return "empty";
  if (agent.status === "approval_needed" || agent.status === "waiting_user") return "needs-user";
  if (agent.status === "error") return "error";
  if (agent.status === "completed_recently") return "completed";
  return "";
}

function appFromBinding(binding) {
  const found = appChoices.find(
    (app) =>
      app.bundleId === binding.bundle_id ||
      app.name === binding.app_name ||
      app.path === binding.app_path,
  );
  if (found) return found;
  const token = binding.icon_token || (binding.app_name || "App").slice(0, 2);
  return {
    name: binding.app_name || binding.label || "App",
    token,
    path: binding.app_path || "",
    bundleId: binding.bundle_id || "",
    color: binding.icon_color || "linear-gradient(135deg, #5a6572, #202832)",
  };
}

function uiKeyFromBinding(binding) {
  const base = { index: binding.index, dirty: false };
  if (binding.kind === "app") {
    return { ...base, role: "quick", kind: "app", app: appFromBinding(binding) };
  }
  if (binding.kind === "url") {
    return { ...base, role: "quick", kind: "url", url: binding.url || "" };
  }
  if (binding.kind === "folder") {
    return { ...base, role: "quick", kind: "folder", path: binding.path || "" };
  }
  if (binding.kind === "agent") {
    return { ...base, role: "agent", kind: "agent", slot: 1 };
  }
  if (binding.kind === "disabled") {
    return { ...base, role: "disabled", kind: "disabled" };
  }
  return { ...base, role: "quick", kind: "unassigned" };
}

function applyKeyLayoutResponse(response) {
  if (!response?.layout?.keys) return;
  state.keyLayoutSource = response.source || "runtime";
  state.keys = response.layout.keys
    .slice()
    .sort((a, b) => a.index - b.index)
    .map(uiKeyFromBinding);
  renumberAgentSlots();
  state.dirty = false;
}

function bindingFromUiKey(key) {
  if (key.kind === "app") {
    return {
      index: key.index,
      kind: "app",
      label: key.app.name,
      app_name: key.app.name,
      app_path: key.app.path,
      bundle_id: key.app.bundleId || null,
      icon_token: key.app.token,
      icon_color: key.app.color,
    };
  }
  if (key.kind === "url") {
    return {
      index: key.index,
      kind: "url",
      label: key.url || "URL",
      url: key.url || "https://agent.deck.local",
    };
  }
  if (key.kind === "folder") {
    return {
      index: key.index,
      kind: "folder",
      label: key.path || "Folder",
      path: key.path || "~/Projects",
    };
  }
  if (key.kind === "agent") {
    return { index: key.index, kind: "agent", label: "Agent" };
  }
  if (key.kind === "disabled") {
    return { index: key.index, kind: "disabled" };
  }
  return { index: key.index, kind: "unassigned" };
}

function renderKeyFace(key) {
  if (key.kind === "unassigned") {
    return '<div class="unassigned-mark">+</div>';
  }
  if (key.kind === "app") {
    const app = key.app;
    if (app.iconUrl) {
      return `<img class="app-icon app-icon-img" src="${escapeAttr(app.iconUrl)}" alt="">`;
    }
    const textColor = app.darkText ? "#15191f" : "#ffffff";
    return `<div class="app-icon" style="background:${app.color};color:${textColor}">${escapeHtml(app.token)}</div>`;
  }
  if (key.kind === "url") {
    return '<div class="url-icon">URL</div>';
  }
  if (key.kind === "folder") {
    return '<div class="folder-icon"></div>';
  }
  if (key.kind === "agent") {
    const agent = agentForSlot(key.slot);
    return `<div class="agent-visual ${agentVisualClass(agent)}"></div>`;
  }
  if (key.kind === "disabled") {
    return '<div class="disabled-key"></div>';
  }
  return "";
}

function renderKeys() {
  el.keyGrid.innerHTML = state.keys
    .map((key) => {
      const selected = key.index === state.selectedIndex ? " selected" : "";
      const dirty = key.dirty ? " dirty" : "";
      return `
        <button class="deck-key${selected}${dirty}" data-key="${key.index}" type="button" aria-label="Key ${key.index + 1} ${keyLabel(key)}">
          <span class="key-inner">${renderKeyFace(key)}</span>
        </button>
      `;
    })
    .join("");

  el.keyGrid.querySelectorAll(".deck-key").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedIndex = Number(button.dataset.key);
      render();
    });
  });
}

function choiceButton(kind, title, meta) {
  const key = state.keys[state.selectedIndex];
  const active = key.kind === kind ? " active" : "";
  return `
    <button class="choice-button${active}" type="button" data-kind="${kind}">
      <span class="choice-title">${title}</span>
      <span class="choice-meta">${meta}</span>
    </button>
  `;
}

function detailRow(label, value) {
  return `<div class="detail-row"><span>${escapeHtml(label)}</span><span title="${escapeAttr(value)}">${escapeHtml(value)}</span></div>`;
}

function renderInspector() {
  const key = state.keys[state.selectedIndex];
  el.selectedEyebrow.textContent = `Key ${key.index + 1}`;
  el.selectedTitle.textContent = keyLabel(key);
  el.selectedSubtitle.textContent =
    key.kind === "agent"
      ? "按键只表达状态，不显示文字或详情。"
      : key.kind === "disabled"
        ? "这个键不会显示内容，也不会响应按下。"
        : "修改只更新 GUI 预览，保存并应用后才下发。";

  let details = "";
  if (key.kind === "app") {
    details =
      detailRow("App", key.app.name) +
      detailRow("路径", key.app.path) +
      detailRow("图标", "使用 App 图标");
  } else if (key.kind === "agent") {
    const agent = agentForSlot(key.slot);
    details =
      detailRow("槽位", `第 ${key.slot} 个 Agent`) +
      detailRow("当前", agent ? agent.status : "空槽") +
      detailRow("按下", "选择并聚焦");
  } else if (key.kind === "url") {
    details = detailRow("网址", key.url || "https://example.com") + detailRow("图标", "自动 favicon");
  } else if (key.kind === "folder") {
    details = detailRow("文件夹", key.path || "~/Projects") + detailRow("图标", "系统文件夹图标");
  }

  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <div class="field-label">用途</div>
      <div class="choice-grid">
        ${choiceButton("app", "打开或切换 App", key.kind === "app" ? key.app.name : "")}
        ${choiceButton("url", "打开网址", "")}
        ${choiceButton("folder", "打开文件夹", "")}
        ${choiceButton("agent", "Agent 状态", key.kind === "agent" ? `槽位 ${key.slot}` : "")}
        ${choiceButton("disabled", "关闭这个键", "")}
      </div>
    </div>
    ${details ? `<div class="field-group"><div class="field-label">当前配置</div>${details}</div>` : ""}
    ${
      key.kind === "app"
        ? '<button id="changeApp" class="ghost-button" type="button">更换 App</button>'
        : key.kind === "unassigned"
          ? '<button id="chooseApp" class="ghost-button" type="button">选择 App</button>'
          : ""
    }
  `;

  el.inspectorBody.querySelectorAll(".choice-button").forEach((button) => {
    button.addEventListener("click", () => updateKeyKind(button.dataset.kind));
  });

  const chooseApp = document.getElementById("chooseApp") || document.getElementById("changeApp");
  if (chooseApp) {
    chooseApp.addEventListener("click", openAppModal);
  }
}

/**
 * 标记当前 GUI 预览已偏离 daemon 配置；只影响保存按钮和状态提示，不直接下发硬件。
 */
function markDirty(key) {
  key.dirty = true;
  state.dirty = true;
  renderSyncState();
}

function updateKeyKind(kind) {
  const key = state.keys[state.selectedIndex];
  if (kind === "app") {
    openAppModal();
    return;
  }
  if (kind === "url") {
    Object.assign(key, { role: "quick", kind: "url", url: "https://agent.deck.local" });
  } else if (kind === "folder") {
    Object.assign(key, { role: "quick", kind: "folder", path: "~/Projects" });
  } else if (kind === "agent") {
    const agentCountBefore = state.keys.filter((item) => item.kind === "agent" && item.index < key.index).length;
    Object.assign(key, { role: "agent", kind: "agent", slot: agentCountBefore + 1 });
    renumberAgentSlots();
  } else if (kind === "disabled") {
    Object.assign(key, { role: "disabled", kind: "disabled" });
  }
  markDirty(key);
  render();
}

function renumberAgentSlots() {
  let slot = 1;
  state.keys.forEach((key) => {
    if (key.kind === "agent") {
      key.slot = slot;
      slot += 1;
    }
  });
}

function openAppModal() {
  state.appQuery = "";
  if (el.appSearch) {
    el.appSearch.value = "";
  }
  renderAppList();
  el.appModal.classList.add("open");
  window.requestAnimationFrame(() => el.appSearch?.focus());
}

/**
 * 关闭 App 选择浮层；不回滚已经写入 GUI 预览的按键配置。
 */
function closeAppModal() {
  el.appModal.classList.remove("open");
}

/**
 * 根据当前搜索词渲染 App 候选项；点击候选项只更新预览，等待保存动作统一下发。
 */
function renderAppList() {
  const query = state.appQuery.trim().toLocaleLowerCase();
  const matches = appChoices
    .map((app, index) => ({ app, index }))
    .filter(({ app }) => {
      if (!query) return true;
      return [app.name, app.path, app.bundleId]
        .filter(Boolean)
        .some((value) => value.toLocaleLowerCase().includes(query));
    });

  el.appCount.textContent = `${matches.length} / ${appChoices.length} 个 App`;

  if (!matches.length) {
    el.appList.innerHTML = '<div class="app-empty">没有匹配的 App</div>';
    return;
  }

  el.appList.innerHTML = matches
    .map(({ app, index }) => {
      const textColor = app.darkText ? "#15191f" : "#ffffff";
      const icon = app.iconUrl
        ? `<img class="app-icon app-icon-img" src="${escapeAttr(app.iconUrl)}" alt="">`
        : `<span class="app-icon" style="background:${app.color};color:${textColor}">${escapeHtml(app.token)}</span>`;
      return `
        <button class="app-option" type="button" data-app="${index}">
          ${icon}
          <span>
            <span class="app-option-name">${escapeHtml(app.name)}</span>
            <span class="app-option-path">${escapeHtml(app.path)}</span>
          </span>
        </button>
      `;
    })
    .join("");

  el.appList.querySelectorAll(".app-option").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const app = appChoices[Number(button.dataset.app)];
      const key = state.keys[state.selectedIndex];
      key.role = "quick";
      key.kind = "app";
      key.app = app;
      markDirty(key);
      closeAppModal();
      render();
      el.toast.textContent = `已选择 ${app.name}，保存并应用后下发`;
      el.saveButton.focus({ preventScroll: true });
    });
  });
}

function renderRuntime() {
  const renderer = state.status?.streamdock_n4pro_renderer;
  const ok = renderer?.last_result?.ok === true;
  const agents = runtimeAgents();
  const panel = state.status?.logical_panel?.selection?.active_kind || "brand";

  el.deviceDot.classList.toggle("connected", ok);
  el.deviceState.textContent = ok ? "N4 Pro · 已连接" : "N4 Pro · 未连接";
  el.rendererState.textContent = ok ? "renderer: ok" : `renderer: ${renderer?.last_error || "unknown"}`;
  el.agentState.textContent = `agents: ${agents.length}`;
  el.panelState.textContent = `panel: ${panel}`;
  el.panelKind.textContent = panel;
  el.panelHint.textContent = panel === "quota" ? "quota panel" : "global panel";
}

function setStatusChip(element, text, variant) {
  element.textContent = text;
  element.classList.remove("ok", "pending", "error");
  if (variant) {
    element.classList.add(variant);
  }
}

/**
 * 将配置保存状态和硬件下发状态合并为一个用户可理解的同步状态。
 */
function renderSyncState() {
  const renderer = state.status?.streamdock_n4pro_renderer;
  const result = renderer?.last_result;
  if (state.saving) {
    setStatusChip(el.syncState, "保存中", "pending");
    el.saveButton.disabled = true;
    return;
  }
  if (state.dirty) {
    setStatusChip(el.syncState, "有未保存改动", "pending");
    el.saveButton.disabled = false;
    return;
  }
  if (renderer?.last_error || result?.ok === false) {
    setStatusChip(el.syncState, "下发失败", "error");
    el.saveButton.disabled = true;
    return;
  }
  if (state.awaitingHardwareApply) {
    setStatusChip(el.syncState, "等待下发", "pending");
    el.saveButton.disabled = true;
    return;
  }
  if (result?.ok === true) {
    setStatusChip(el.syncState, "已下发", "ok");
    el.saveButton.disabled = true;
    return;
  }
  setStatusChip(el.syncState, "检查中", "");
  el.saveButton.disabled = true;
}

function render() {
  renderKeys();
  renderInspector();
  renderRuntime();
  renderSyncState();
}

function reconcileHardwareApply() {
  if (!state.awaitingHardwareApply || state.lastSaveStartedAt === null) return;
  const renderer = state.status?.streamdock_n4pro_renderer;
  const updatedAt = renderer?.updated_at ? Date.parse(renderer.updated_at) : NaN;
  if (renderer?.last_result?.ok === true && Number.isFinite(updatedAt) && updatedAt >= state.lastSaveStartedAt) {
    state.awaitingHardwareApply = false;
  }
}

async function refreshStatus() {
  try {
    const response = await fetch("/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`status ${response.status}`);
    state.status = await response.json();
    reconcileHardwareApply();
    render();
  } catch (error) {
    el.deviceState.textContent = "N4 Pro · 状态不可用";
    el.rendererState.textContent = `renderer: ${error.message}`;
  }
}

async function loadKeyLayout() {
  try {
    const response = await fetch("/ui/key-layout", { cache: "no-store" });
    if (!response.ok) throw new Error(`key layout ${response.status}`);
    const body = await response.json();
    applyKeyLayoutResponse(body);
    render();
  } catch (error) {
    el.toast.textContent = `布局读取失败：${error.message}`;
  }
}

async function loadAppCatalog() {
  try {
    const response = await fetch("/ui/apps", { cache: "no-store" });
    if (!response.ok) throw new Error(`apps ${response.status}`);
    const body = await response.json();
    if (!Array.isArray(body.apps) || body.apps.length === 0) return;
    appChoices = body.apps.map((app) => ({
      name: app.name,
      token: app.icon_token || (app.name || "App").slice(0, 2),
      path: app.app_path || "",
      bundleId: app.bundle_id || "",
      iconUrl: app.icon_url || app.icon_data_url || "",
      keyIconUrl: app.key_icon_url || "",
      color: "linear-gradient(135deg, #5a6572, #202832)",
    }));
    refreshConfiguredAppIcons();
  } catch (error) {
    el.toast.textContent = `App 列表读取失败：${error.message}`;
  }
}

function refreshConfiguredAppIcons() {
  state.keys.forEach((key) => {
    if (key.kind !== "app") return;
    const found = appChoices.find(
      (app) =>
        app.bundleId === key.app.bundleId ||
        app.path === key.app.path ||
        app.name === key.app.name,
    );
    if (found) {
      key.app = found;
    }
  });
}

async function refreshAppIcons() {
  state.refreshingApps = true;
  el.refreshApps.disabled = true;
  el.toast.textContent = "正在刷新 App 图标";
  try {
    const response = await fetch("/ui/apps/refresh-icons", {
      method: "POST",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`refresh ${response.status}`);
    await loadAppCatalog();
    renderAppList();
    render();
    el.toast.textContent = "App 图标已刷新";
  } catch (error) {
    el.toast.textContent = `App 图标刷新失败：${error.message}`;
  } finally {
    state.refreshingApps = false;
    el.refreshApps.disabled = false;
    window.setTimeout(() => {
      if (!state.refreshingApps) el.toast.textContent = "";
    }, 2400);
  }
}

/**
 * 保存当前 GUI 预览并请求 daemon 应用到硬件；失败时保留本地状态并提示错误。
 */
async function saveAndApply() {
  const saveStartedAt = Date.now();
  state.saving = true;
  renderSyncState();
  el.toast.textContent = "";
  try {
    const response = await fetch("/ui/key-layout", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        keys: state.keys
          .slice()
          .sort((a, b) => a.index - b.index)
          .map(bindingFromUiKey),
      }),
    });
    if (!response.ok) throw new Error(`save ${response.status}`);
    const body = await response.json();
    applyKeyLayoutResponse(body.key_layout);
    state.keys.forEach((key) => {
      key.dirty = false;
    });
    state.dirty = false;
    state.lastSaveStartedAt = saveStartedAt;
    state.awaitingHardwareApply = true;
    el.toast.textContent = "配置已保存，等待硬件下发";
    await refreshStatus();
  } catch (error) {
    state.awaitingHardwareApply = false;
    el.toast.textContent = `保存失败：${error.message}`;
  } finally {
    state.saving = false;
    render();
    window.setTimeout(() => {
      el.toast.textContent = "";
    }, 3200);
  }
}

el.saveButton.addEventListener("click", saveAndApply);
el.closeAppModal.addEventListener("click", closeAppModal);
el.appModal.addEventListener("click", (event) => {
  if (event.target === el.appModal) closeAppModal();
});
el.appSearch.addEventListener("input", () => {
  state.appQuery = el.appSearch.value;
  renderAppList();
});
el.refreshApps.addEventListener("click", refreshAppIcons);

async function boot() {
  await loadAppCatalog();
  await loadKeyLayout();
  await refreshStatus();
  render();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}

render();
boot();
window.setInterval(refreshStatus, 5000);
