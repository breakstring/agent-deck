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

const THEME_STORAGE_KEY = "agentDeckTheme";

/**
 * 读取用户上次选择的页面主题；localStorage 不可用或值非法时回退到暗色主题。
 */
function readStoredTheme() {
  try {
    const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
    return storedTheme === "light" || storedTheme === "dark" ? storedTheme : "dark";
  } catch (_error) {
    return "dark";
  }
}

/**
 * 将页面主题写入浏览器本地存储；写入失败不阻断本次 UI 交互。
 */
function persistTheme(theme) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (_error) {
    // localStorage 不可用时仍允许本次页面内切换主题。
  }
}

const fallbackKeys = Array.from({ length: 10 }, (_, index) => {
  if (index < 5) {
    return { index, role: "quick", kind: "unassigned", dirty: false };
  }
  return { index, role: "agent", kind: "agent", slot: index - 4, dirty: false };
});

const fallbackRotaryLayout = {
  controls: [
    { control_id: "knob_1", rotate_action: "cycle_virtual_panel" },
    { control_id: "knob_2", rotate_action: "unassigned" },
    { control_id: "knob_3", rotate_action: "unassigned" },
    { control_id: "knob_4", rotate_action: "cycle_panel_content" },
  ],
  lighting: { mode: "off", color: null, breathe: false },
  console_brightness_percent: 100,
  system_display_id: null,
};

const state = {
  keys: fallbackKeys,
  selectedIndex: 0,
  selectedSurface: "key",
  selectedKnobId: "knob_1",
  rotaryLayout: structuredClone(fallbackRotaryLayout),
  rotaryLayoutSource: "default",
  controlCapabilities: null,
  appQuery: "",
  dirty: false,
  status: null,
  saving: false,
  refreshingApps: false,
  awaitingHardwareApply: false,
  theme: readStoredTheme(),
  lastSaveStartedAt: null,
  keyLayoutSource: "default",
  urlIconCache: new Map(),
};

const el = {
  keyGrid: document.getElementById("keyGrid"),
  knobStrip: document.getElementById("knobStrip"),
  lightingControl: document.getElementById("lightingControl"),
  selectedEyebrow: document.getElementById("selectedEyebrow"),
  selectedTitle: document.getElementById("selectedTitle"),
  selectedSubtitle: document.getElementById("selectedSubtitle"),
  inspectorBody: document.getElementById("inspectorBody"),
  saveButton: document.getElementById("saveButton"),
  themeToggle: document.getElementById("themeToggle"),
  themeToggleIcon: document.getElementById("themeToggleIcon"),
  syncState: document.getElementById("syncState"),
  deviceDot: document.getElementById("deviceDot"),
  deviceState: document.getElementById("deviceState"),
  rendererState: document.getElementById("rendererState"),
  agentState: document.getElementById("agentState"),
  panelState: document.getElementById("panelState"),
  toast: document.getElementById("toast"),
  appModal: document.getElementById("appModal"),
  appSearch: document.getElementById("appSearch"),
  refreshApps: document.getElementById("refreshApps"),
  appCount: document.getElementById("appCount"),
  appList: document.getElementById("appList"),
  closeAppModal: document.getElementById("closeAppModal"),
};

/**
 * 根据当前主题刷新右上角切换按钮的文案、图标和无障碍状态。
 */
function updateThemeToggle() {
  if (!el.themeToggle) return;
  const isLight = state.theme === "light";
  el.themeToggle.setAttribute("aria-pressed", String(isLight));
  el.themeToggle.setAttribute("aria-label", isLight ? "切换到暗色主题" : "切换到亮色主题");
  el.themeToggle.title = isLight ? "切换到暗色主题" : "切换到亮色主题";
  if (el.themeToggleIcon) el.themeToggleIcon.textContent = isLight ? "☾" : "☀";
}

/**
 * 应用页面主题并按需持久化；只影响配置 GUI，不改变硬件下发布局。
 */
function applyTheme(theme, options = {}) {
  const resolvedTheme = theme === "light" ? "light" : "dark";
  state.theme = resolvedTheme;
  document.documentElement.dataset.theme = resolvedTheme;
  document.documentElement.style.colorScheme = resolvedTheme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", resolvedTheme === "light" ? "#f4f7fb" : "#0b0d10");
  updateThemeToggle();
  if (options.persist) persistTheme(resolvedTheme);
}

function keyLabel(key) {
  if (key.kind === "unassigned") return "未设置快捷动作";
  if (key.kind === "app") return "打开或切换 App";
  if (key.kind === "url") return "打开网址";
  if (key.kind === "agent") return "Agent 状态槽位";
  if (key.kind === "quota_status") return "订阅 / 限额状态";
  if (key.kind === "usage_summary") return "Token / 金额用量";
  if (key.kind === "disabled") return "暂不设定";
  return "按键";
}

function quotaWindowLabel(value) {
  if (!value || value === "auto") return "AUTO";
  const windows =
    state.status?.codex_quota?.display_snapshot?.windows ||
    state.status?.codex_quota?.snapshot?.windows ||
    [];
  const selected = windows.find(
    (item) => item.window_id === value || item.source_slot === value,
  );
  if (!selected) return "AUTO";
  const minutes = Number(selected.window_duration_mins || 0);
  let period = `${minutes}M`;
  if (minutes >= 28 * 24 * 60 && minutes <= 31 * 24 * 60) {
    period = "MONTH";
  } else if (minutes > 0 && minutes % (7 * 24 * 60) === 0) {
    const weeks = minutes / (7 * 24 * 60);
    period = weeks === 1 ? "WEEK" : `${weeks}W`;
  } else if (minutes > 0 && minutes % (24 * 60) === 0) {
    const days = minutes / (24 * 60);
    period = days === 1 ? "DAY" : `${days}D`;
  }
  else if (minutes > 0 && minutes % 60 === 0) period = `${minutes / 60}H`;
  if (minutes <= 0) return "AUTO";
  const label = selected.presentation_label || selected.limit_name;
  return label ? `${label} · ${period}` : period;
}

function usagePeriodLabel(value) {
  if (value === "week") return "Week";
  if (value === "month") return "Month";
  if (value === "all") return "All";
  return "Day";
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
    return {
      ...base,
      role: "quick",
      kind: "url",
      url: binding.url || "",
      iconUrl: "",
      iconToken: tokenForUrl(binding.url),
      iconStatus: "使用域名缩写",
      iconLoading: false,
    };
  }
  if (binding.kind === "agent") {
    return { ...base, role: "agent", kind: "agent", slot: 1 };
  }
  if (binding.kind === "quota_status") {
    return {
      ...base,
      role: "status",
      kind: "quota_status",
      quotaWindow: binding.quota_window || "auto",
    };
  }
  if (binding.kind === "usage_summary") {
    return {
      ...base,
      role: "status",
      kind: "usage_summary",
      usagePeriod: binding.usage_period || "today",
    };
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
  refreshConfiguredUrlIcons();
  state.dirty = false;
}

/** 将 daemon rotary layout 复制成可编辑草稿，不会改变已应用的真实硬件状态。 */
function applyRotaryLayoutResponse(response) {
  if (!response?.layout?.controls) return;
  state.rotaryLayoutSource = response.source || "runtime";
  state.rotaryLayout = structuredClone(response.layout);
}

/** 返回一个旋钮位置的草稿 binding。 */
function rotaryBinding(controlId) {
  return state.rotaryLayout.controls.find((binding) => binding.control_id === controlId) || null;
}

/** 将 HTML 十六进制基础色转换成设备预览用的 `r, g, b` CSS 三元组。 */
function rgbForLightingPreview() {
  const color = state.rotaryLayout.lighting?.color;
  if (state.rotaryLayout.lighting?.mode !== "color" || !/^#[0-9a-f]{6}$/i.test(color || "")) {
    return "111, 213, 255";
  }
  return [1, 3, 5]
    .map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16))
    .join(", ");
}

/** 返回旋钮按下由当前旋转用途隐式决定的说明，不把它暴露成独立配置。 */
function impliedPressDescription(rotateAction) {
  if (rotateAction === "adjust_output_volume") return "切换输出静音";
  if (rotateAction === "adjust_input_volume") return "切换麦克风静音";
  return "不执行动作";
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
  if (key.kind === "agent") {
    return { index: key.index, kind: "agent", label: "Agent" };
  }
  if (key.kind === "quota_status") {
    return {
      index: key.index,
      kind: "quota_status",
      label: "订阅 / 限额状态",
      quota_window: key.quotaWindow || "auto",
    };
  }
  if (key.kind === "usage_summary") {
    return {
      index: key.index,
      kind: "usage_summary",
      label: "Token / 金额用量",
      usage_period: key.usagePeriod || "today",
    };
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
    if (key.iconUrl) {
      return `<img class="url-icon-img" src="${escapeAttr(key.iconUrl)}" alt="">`;
    }
    return `<div class="url-icon">${escapeHtml(key.iconToken || tokenForUrl(key.url))}</div>`;
  }
  if (key.kind === "agent") {
    const agent = agentForSlot(key.slot);
    return `<div class="agent-visual ${agentVisualClass(agent)}"></div>`;
  }
  if (key.kind === "quota_status") {
    return `
      <div class="status-key-preview quota-status-preview">
        <span>${escapeHtml(quotaWindowLabel(key.quotaWindow))}</span>
        <strong>Quota</strong>
      </div>
    `;
  }
  if (key.kind === "usage_summary") {
    return `
      <div class="status-key-preview usage-summary-preview">
        <span>${escapeHtml(usagePeriodLabel(key.usagePeriod))}</span>
        <strong>Usage</strong>
      </div>
    `;
  }
  if (key.kind === "disabled") {
    return '<div class="disabled-key"></div>';
  }
  return "";
}

function renderKeys() {
  el.keyGrid.innerHTML = state.keys
    .map((key) => {
      const selected = state.selectedSurface === "key" && key.index === state.selectedIndex ? " selected" : "";
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
      state.selectedSurface = "key";
      render();
    });
  });
}

/**
 * 渲染 N4 Pro 四个可配置旋钮的选中态和当前灯圈组草稿预览。
 * 当前 profile 的灯光为 group，因此四个 `.led` 必须同步更新，不能显示独立颜色。
 */
function renderKnobs() {
  const ledColor = rgbForLightingPreview();
  const ledOpacity = state.rotaryLayout.lighting?.mode === "color" ? "0.9" : "0";
  el.knobStrip.style.setProperty("--n4pro-led-color", ledColor);
  el.knobStrip.style.setProperty("--n4pro-led-opacity", ledOpacity);
  el.knobStrip.classList.toggle("breathing", state.rotaryLayout.lighting?.breathe === true && ledOpacity !== "0");
  el.knobStrip.querySelectorAll(".knob-cell").forEach((button) => {
    const selected = state.selectedSurface === "rotary" && button.dataset.knob === state.selectedKnobId;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.onclick = () => {
      state.selectedSurface = "rotary";
      state.selectedKnobId = button.dataset.knob;
      render();
    };
  });
  el.lightingControl.classList.toggle("selected", state.selectedSurface === "lighting");
  el.lightingControl.setAttribute("aria-pressed", String(state.selectedSurface === "lighting"));
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

function textField(id, label, value, placeholder) {
  return `
    <label class="text-field" for="${id}">
      <span>${escapeHtml(label)}</span>
      <input id="${id}" class="text-input" type="text" value="${escapeAttr(value)}" placeholder="${escapeAttr(placeholder)}" autocomplete="off" spellcheck="false">
    </label>
  `;
}

function renderUrlIconControls(key) {
  const preview = key.iconUrl
    ? `<img class="url-icon-preview-img" src="${escapeAttr(key.iconUrl)}" alt="">`
    : `<div class="url-icon-preview-token">${escapeHtml(key.iconToken || tokenForUrl(key.url))}</div>`;
  const disabled = key.iconLoading ? " disabled" : "";
  return `
    <div class="field-group">
      <div class="field-label">图标</div>
      <div class="url-icon-control">
        <div class="url-icon-preview">${preview}</div>
        <div class="url-icon-actions">
          <button id="parseUrlIcon" class="ghost-button compact" type="button"${disabled}>解析网页图标</button>
          <button id="chooseUrlIcon" class="ghost-button compact" type="button"${disabled}>选择图片</button>
          <input id="urlIconFile" class="hidden-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/x-icon">
        </div>
      </div>
      <div class="url-icon-status">${escapeHtml(key.iconLoading ? "正在处理图标" : key.iconStatus || "使用域名缩写")}</div>
    </div>
  `;
}

function renderKeyInspector() {
  const key = state.keys[state.selectedIndex];
  el.selectedEyebrow.textContent = `Key ${key.index + 1}`;
  el.selectedTitle.textContent = keyLabel(key);
  el.selectedSubtitle.textContent =
    key.kind === "agent"
      ? "按键只表达状态，不显示文字或详情。"
      : key.kind === "quota_status"
        ? "按下后切换 Auto、5H、Week 的订阅状态图。"
        : key.kind === "usage_summary"
          ? "按下后切换 Day、Week、Month、All 的 Token/金额统计图。"
      : key.kind === "disabled"
        ? "这个键暂不显示内容，也不会响应按下。"
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
    details = detailRow("网址", key.url || "https://example.com") + detailRow("图标", key.iconStatus || "使用域名缩写");
  } else if (key.kind === "quota_status") {
    details =
      detailRow("展示", quotaWindowLabel(key.quotaWindow)) +
      detailRow("按下", "切换 Auto / 当前可用限额") +
      detailRow("数据", "复用 touch bar quota 快照");
  } else if (key.kind === "usage_summary") {
    details =
      detailRow("周期", usagePeriodLabel(key.usagePeriod)) +
      detailRow("按下", "切换 Day / Week / Month / All") +
      detailRow("数据", "复用 touch bar ccusage 快照");
  }

  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <div class="field-label">用途</div>
      <div class="choice-grid">
        ${choiceButton("app", "打开或切换 App", key.kind === "app" ? key.app.name : "")}
        ${choiceButton("url", "打开网址", "")}
        ${choiceButton("quota_status", "订阅 / 限额状态", key.kind === "quota_status" ? quotaWindowLabel(key.quotaWindow) : "")}
        ${choiceButton("usage_summary", "Token / 金额用量", key.kind === "usage_summary" ? usagePeriodLabel(key.usagePeriod) : "")}
        ${choiceButton("agent", "Agent 状态", key.kind === "agent" ? `槽位 ${key.slot}` : "")}
        ${choiceButton("disabled", "暂不设定", "")}
      </div>
    </div>
    ${
      key.kind === "url"
        ? `<div class="field-group"><div class="field-label">目标</div>${textField("urlInput", "网址", key.url || "", "https://example.com")}</div>`
        : ""
    }
    ${
      key.kind === "url"
        ? renderUrlIconControls(key)
        : ""
    }
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

  const urlInput = document.getElementById("urlInput");
  if (urlInput) {
    urlInput.addEventListener("input", () => {
      key.url = urlInput.value.trim();
      key.iconUrl = "";
      key.iconToken = tokenForUrl(key.url);
      key.iconStatus = "使用域名缩写";
      markDirty(key);
    });
  }

  const parseUrlIcon = document.getElementById("parseUrlIcon");
  if (parseUrlIcon) {
    parseUrlIcon.addEventListener("click", () => resolveUrlIconForKey(key.index, key.url, { force: true }));
  }

  const chooseUrlIcon = document.getElementById("chooseUrlIcon");
  const urlIconFile = document.getElementById("urlIconFile");
  if (chooseUrlIcon && urlIconFile) {
    chooseUrlIcon.addEventListener("click", () => urlIconFile.click());
    urlIconFile.addEventListener("change", () => {
      const file = urlIconFile.files?.[0];
      if (file) uploadUrlIconForKey(key.index, key.url, file);
      urlIconFile.value = "";
    });
  }

}

/**
 * 返回旋钮动作下拉框所需的稳定中文文案；系统显示器亮度仅在 capability scan 有目标时显示。
 */
function rotateActionOptions() {
  const options = [
    ["unassigned", "暂不设定"],
    ["cycle_virtual_panel", "切换虚拟面板"],
    ["cycle_panel_content", "切换面板内容"],
    ["adjust_output_volume", "调节输出音量"],
    ["adjust_input_volume", "调节输入音量"],
    ["adjust_deck_display_brightness", "调节控制台屏幕亮度"],
  ];
  if ((state.controlCapabilities?.system_display_targets || []).length > 0) {
    options.splice(5, 0, ["adjust_system_display_brightness", "调节系统显示器亮度"]);
  }
  return options;
}

/**
 * 生成选中指定枚举动作的原生下拉项 HTML。
 */
function actionOptionsHtml(options, current) {
  return options
    .map(([value, label]) => `<option value="${escapeAttr(value)}"${value === current ? " selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");
}

/**
 * 渲染单个旋钮检查器；按下语义从旋转用途派生，不允许组合出难以理解的跨用途动作。
 */
function renderRotaryInspector() {
  const controlId = state.selectedKnobId;
  const binding = rotaryBinding(controlId);
  const capability = state.controlCapabilities?.device_profile?.rotary?.controls?.find((item) => item.id === controlId);
  const controlNumber = controlId?.replace("knob_", "") || "";
  el.selectedEyebrow.textContent = `旋钮 ${controlNumber}`;
  el.selectedTitle.textContent = "旋钮配置";
  el.selectedSubtitle.textContent = "设置左右旋转用途；按下语义会依据音量用途自动决定，修改只更新预览。";
  if (!binding) {
    el.inspectorBody.innerHTML = '<p class="control-note">当前硬件 profile 未提供这个旋钮。</p>';
    return;
  }
  const supportsPress = capability?.supports_press !== false;
  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <label class="text-field" for="rotateAction"><span>左右旋转</span>
        <select id="rotateAction" class="select-input">${actionOptionsHtml(rotateActionOptions(), binding.rotate_action)}</select>
      </label>
    </div>
    <div class="field-group"><div class="field-label">当前能力</div>
      ${detailRow("旋转", capability?.supports_rotate === false ? "不支持" : "支持")}
      ${detailRow("按下", supportsPress ? impliedPressDescription(binding.rotate_action) : "不支持")}
    </div>
  `;
  document.getElementById("rotateAction")?.addEventListener("change", (event) => {
    binding.rotate_action = event.target.value;
    markRotaryDirty();
    render();
  });
}

/**
 * 渲染独立的控制台灯光入口；N4 Pro 预览始终以一个 group 同步四个灯圈。
 */
function renderLightingInspector() {
  const lighting = state.rotaryLayout.lighting || { mode: "off", color: null, breathe: false };
  const targets = state.controlCapabilities?.system_display_targets || [];
  el.selectedEyebrow.textContent = "控制台灯光";
  el.selectedTitle.textContent = "旋钮灯圈组";
  el.selectedSubtitle.textContent = "N4 Pro 当前只能统一设置四个旋钮灯圈；颜色编辑会立即更新预览。";
  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <label class="text-field" for="lightingMode"><span>灯光</span>
        <select id="lightingMode" class="select-input">
          <option value="off"${lighting.mode === "off" ? " selected" : ""}>关闭</option>
          <option value="color"${lighting.mode === "color" ? " selected" : ""}>指定颜色</option>
        </select>
      </label>
    </div>
    ${lighting.mode === "color" ? `
      <div class="field-group">
        <div class="field-label">基础色</div>
        <div class="color-setting">
          <input id="lightingColor" class="color-input" type="color" value="${escapeAttr(lighting.color || "#35C9FF")}" aria-label="灯圈基础色">
          <span class="detail-row"><span>预览</span><span>${escapeHtml((lighting.color || "#35C9FF").toUpperCase())}</span></span>
        </div>
      </div>` : ""}
    ${lighting.mode === "color" ? `
      <label class="switch-field" for="lightingBreathe">
        <span><strong>柔和呼吸</strong><small>四个灯圈以同一基础色同步变化</small></span>
        <input id="lightingBreathe" type="checkbox"${lighting.breathe ? " checked" : ""}>
      </label>` : ""}
    ${targets.length ? `
      <div class="field-group">
        <label class="text-field" for="systemDisplayTarget"><span>系统显示器亮度目标</span>
          <select id="systemDisplayTarget" class="select-input">
            <option value="">未选择</option>
            ${targets.map((target) => `<option value="${escapeAttr(target.id)}"${target.id === state.rotaryLayout.system_display_id ? " selected" : ""}>${escapeHtml(target.label)}</option>`).join("")}
          </select>
        </label>
      </div>` : ""}
  `;
  document.getElementById("lightingMode")?.addEventListener("change", (event) => {
    const mode = event.target.value;
    state.rotaryLayout.lighting = mode === "color"
      ? { mode: "color", color: lighting.color || "#35C9FF", breathe: lighting.breathe === true }
      : { mode: "off", color: null, breathe: false };
    markRotaryDirty();
    render();
  });
  document.getElementById("lightingColor")?.addEventListener("input", (event) => {
    state.rotaryLayout.lighting = { mode: "color", color: event.target.value.toUpperCase(), breathe: lighting.breathe === true };
    markRotaryDirty();
    render();
  });
  document.getElementById("lightingBreathe")?.addEventListener("change", (event) => {
    state.rotaryLayout.lighting = {
      mode: "color",
      color: lighting.color || "#35C9FF",
      breathe: event.target.checked,
    };
    markRotaryDirty();
    render();
  });
  document.getElementById("systemDisplayTarget")?.addEventListener("change", (event) => {
    state.rotaryLayout.system_display_id = event.target.value || null;
    markRotaryDirty();
    render();
  });
}

/**
 * 根据当前设备预览选中的表面渲染 key、旋钮或独立灯光设置检查器。
 */
function renderInspector() {
  if (state.selectedSurface === "rotary") {
    renderRotaryInspector();
    return;
  }
  if (state.selectedSurface === "lighting") {
    renderLightingInspector();
    return;
  }
  renderKeyInspector();
}

/**
 * 标记当前 GUI 预览已偏离 daemon 配置；只影响保存按钮和状态提示，不直接下发硬件。
 */
function markDirty(key) {
  key.dirty = true;
  state.dirty = true;
  renderSyncState();
}

/** 标记旋钮、灯光或系统显示器目标草稿已变化，硬件保持当前已应用状态。 */
function markRotaryDirty() {
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
    Object.assign(key, {
      role: "quick",
      kind: "url",
      url: "https://agent.deck.local",
      iconUrl: "",
      iconToken: "AD",
      iconStatus: "使用域名缩写",
      iconLoading: false,
    });
  } else if (kind === "agent") {
    const agentCountBefore = state.keys.filter((item) => item.kind === "agent" && item.index < key.index).length;
    Object.assign(key, { role: "agent", kind: "agent", slot: agentCountBefore + 1 });
    renumberAgentSlots();
  } else if (kind === "quota_status") {
    Object.assign(key, {
      role: "status",
      kind: "quota_status",
      quotaWindow: key.quotaWindow || "auto",
    });
  } else if (kind === "usage_summary") {
    Object.assign(key, {
      role: "status",
      kind: "usage_summary",
      usagePeriod: key.usagePeriod || "today",
    });
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
  renderKnobs();
  renderInspector();
  renderRuntime();
  renderSyncState();
}

function isEditingInspectorField() {
  const active = document.activeElement;
  if (!active || !el.inspectorBody.contains(active)) return false;
  return active.matches("input, textarea, select, [contenteditable='true']");
}

function renderPassiveRuntimeRefresh() {
  renderKeys();
  renderKnobs();
  renderRuntime();
  renderSyncState();
  if (!isEditingInspectorField()) {
    renderInspector();
  }
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
    renderPassiveRuntimeRefresh();
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

/** 从 daemon 读取已应用的旋钮布局，用于初始化 GUI 草稿。 */
async function loadRotaryLayout() {
  try {
    const response = await fetch("/ui/rotary-layout", { cache: "no-store" });
    if (!response.ok) throw new Error(`rotary layout ${response.status}`);
    applyRotaryLayoutResponse(await response.json());
    render();
  } catch (error) {
    el.toast.textContent = `旋钮配置读取失败：${error.message}`;
  }
}

/** 读取硬件 profile 与本机系统控制 capability，借此过滤不可用动作。 */
async function loadControlCapabilities() {
  try {
    const response = await fetch("/ui/control-capabilities", { cache: "no-store" });
    if (!response.ok) throw new Error(`control capabilities ${response.status}`);
    state.controlCapabilities = await response.json();
    render();
  } catch (error) {
    el.toast.textContent = `控制能力读取失败：${error.message}`;
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

function refreshConfiguredUrlIcons() {
  state.keys.forEach((key) => {
    if (key.kind === "url") {
      key.iconToken = tokenForUrl(key.url);
      lookupCachedUrlIconForKey(key.index, key.url);
    }
  });
}

async function lookupCachedUrlIconForKey(index, url) {
  if (!isHttpUrl(url)) return;
  try {
    const response = await fetch(`/ui/url-icons/lookup?url=${encodeURIComponent(url)}`, {
      cache: "no-store",
    });
    if (!response.ok) return;
    const body = await response.json();
    state.urlIconCache.set(url, body);
    const key = state.keys.find((item) => item.index === index);
    if (!key || key.kind !== "url" || key.url !== url) return;
    if (body.icon_url) {
      applyUrlIconToKey(key, body);
    }
    renderPassiveRuntimeRefresh();
  } catch (_error) {
    // 缓存查询失败时保持 token fallback。
  }
}

async function resolveUrlIconForKey(index, url, options = {}) {
  if (!isHttpUrl(url)) {
    el.toast.textContent = "请输入 http/https 网址";
    return;
  }
  const key = state.keys.find((item) => item.index === index);
  if (!key || key.kind !== "url") return;
  key.iconLoading = true;
  key.iconStatus = "正在解析网页图标";
  render();
  try {
    const force = options.force ? "&force=true" : "";
    const response = await fetch(`/ui/url-icons/resolve?url=${encodeURIComponent(url)}${force}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`resolve ${response.status}`);
    const body = await response.json();
    state.urlIconCache.set(url, body);
    const current = state.keys.find((item) => item.index === index);
    if (!current || current.kind !== "url" || current.url !== url) return;
    applyUrlIconToKey(current, body);
    markDirty(current);
    render();
  } catch (error) {
    const current = state.keys.find((item) => item.index === index);
    if (current && current.kind === "url" && current.url === url) {
      current.iconStatus = `解析失败：${error.message}`;
      current.iconUrl = "";
      current.iconToken = tokenForUrl(url);
      render();
    }
  } finally {
    const current = state.keys.find((item) => item.index === index);
    if (current && current.kind === "url" && current.url === url) {
      current.iconLoading = false;
      render();
    }
  }
}

async function uploadUrlIconForKey(index, url, file) {
  if (!isHttpUrl(url)) {
    el.toast.textContent = "请先输入 http/https 网址";
    return;
  }
  const key = state.keys.find((item) => item.index === index);
  if (!key || key.kind !== "url") return;
  key.iconLoading = true;
  key.iconStatus = "正在导入本地图片";
  render();
  try {
    const dataUrl = await readFileAsDataUrl(file);
    const response = await fetch("/ui/url-icons/upload", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        url,
        filename: file.name,
        data_url: dataUrl,
      }),
    });
    if (!response.ok) throw new Error(`upload ${response.status}`);
    const body = await response.json();
    state.urlIconCache.set(url, body);
    const current = state.keys.find((item) => item.index === index);
    if (!current || current.kind !== "url" || current.url !== url) return;
    applyUrlIconToKey(current, body);
    markDirty(current);
    render();
  } catch (error) {
    const current = state.keys.find((item) => item.index === index);
    if (current && current.kind === "url" && current.url === url) {
      current.iconStatus = `导入失败：${error.message}`;
      render();
    }
  } finally {
    const current = state.keys.find((item) => item.index === index);
    if (current && current.kind === "url" && current.url === url) {
      current.iconLoading = false;
      render();
    }
  }
}

function applyUrlIconToKey(key, icon) {
  key.iconUrl = icon.icon_url || "";
  key.iconToken = icon.icon_token || tokenForUrl(key.url);
  if (icon.icon_cache_source === "custom_upload") {
    key.iconStatus = "使用自定义图片";
  } else if (icon.icon_cache_status === "ready") {
    key.iconStatus = `已缓存 ${icon.host || "网站"} 图标`;
  } else if (icon.icon_cache_status === "fallback") {
    key.iconStatus = "未找到可用图标，使用缩写";
  } else {
    key.iconStatus = "使用域名缩写";
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(new Error("file read failed")));
    reader.readAsDataURL(file);
  });
}

function isHttpUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch (_error) {
    return false;
  }
}

function tokenForUrl(value) {
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.replace(/^www\./i, "");
    const firstLabel = host.split(".")[0] || "URL";
    const compact = firstLabel.replace(/[^a-z0-9]/gi, "").toUpperCase();
    if (!compact) return "URL";
    return compact.length <= 3 ? compact : compact.slice(0, 2);
  } catch (_error) {
    return "URL";
  }
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
    const response = await fetch("/ui/configuration", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        key_layout: {
          keys: state.keys
            .slice()
            .sort((a, b) => a.index - b.index)
            .map(bindingFromUiKey),
        },
        rotary_layout: state.rotaryLayout,
      }),
    });
    if (!response.ok) throw new Error(`save ${response.status}`);
    const body = await response.json();
    applyKeyLayoutResponse(body.key_layout);
    applyRotaryLayoutResponse(body.rotary_layout);
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
el.themeToggle?.addEventListener("click", () => {
  applyTheme(state.theme === "light" ? "dark" : "light", { persist: true });
});
el.lightingControl?.addEventListener("click", () => {
  state.selectedSurface = "lighting";
  render();
});
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
  await loadRotaryLayout();
  await loadControlCapabilities();
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

applyTheme(state.theme);
render();
boot();
window.setInterval(refreshStatus, 5000);
