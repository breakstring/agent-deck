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
  petsPanelSettings: {
    remote_pet_source: "builtin_random",
    patrol_speed: "medium",
  },
  petsPanelSettingsSource: "default",
  displayAppearance: {
    background_color: null,
  },
  displayAppearanceSource: "default",
  displayAppearanceRevision: 0,
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
  shortcutRecordingIndex: null,
  shortcutManualOpenIndex: null,
  shortcutPermissionDetailsOpen: false,
  shortcutPermissionAction: null,
  shortcutPermissionFeedback: null,
};

const el = {
  keyGrid: document.getElementById("keyGrid"),
  knobStrip: document.getElementById("knobStrip"),
  lightingControl: document.getElementById("lightingControl"),
  petsPanelControl: document.getElementById("petsPanelControl"),
  appearanceControl: document.getElementById("appearanceControl"),
  devicePreview: document.querySelector(".n4-pro"),
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

/** 返回硬件按键 DOM 对应的稳定位置标识。 */
function keySwapItemId(item) {
  return Number(item.dataset.key);
}

/** 只允许已有操作定义且当前未保存、未录制快捷键的主键发起拖拽。 */
function canDragConfiguredKey(item) {
  const key = state.keys.find((candidate) => candidate.index === keySwapItemId(item));
  return item.dataset.swapEnabled === "true"
    && !state.saving
    && state.shortcutRecordingIndex === null
    && !keyHasPendingAssetWork(key);
}

/** 拒绝把操作放到仍在解析或上传图标的目标键，避免异步回调按旧位置回写。 */
function canDropOnKey(_source, target) {
  const key = state.keys.find((candidate) => candidate.index === keySwapItemId(target));
  return Boolean(key) && !keyHasPendingAssetWork(key);
}

/** 判断一个按键草稿是否仍有依赖物理 index 的图标异步任务。 */
function keyHasPendingAssetWork(key) {
  return key?.iconLoading === true || key?.shortcutIconLoading === true;
}

/** 把通用表面控制器的有效放置结果转换成按键操作交换。 */
function handleKeySwap({ sourceId, targetId }) {
  swapKeyOperations(Number(sourceId), Number(targetId));
}

/** 对越界、空隙或取消放置给出短暂说明，不修改按键草稿。 */
function handleRejectedKeySwap({ reason }) {
  if (reason === "cancelled") return;
  const message = reason === "outside-boundary"
    ? "按键只能在主键区域内交换"
    : "拖到另一个按键上即可交换操作";
  showTransientToast(message, 2200);
}

const keySwapController = window.AgentDeckSurfaceSwap.createBoundedSwapController({
  container: el.keyGrid,
  itemSelector: ".deck-key",
  getItemId: keySwapItemId,
  isDraggable: canDragConfiguredKey,
  canDrop: canDropOnKey,
  onSwap: handleKeySwap,
  onDropRejected: handleRejectedKeySwap,
});

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

/** 返回主键用途的中文标题；宠物键只描述展示用途，不暗示可点击动作。 */
function keyLabel(key) {
  if (key.kind === "unassigned") return "未设置快捷动作";
  if (key.kind === "app") return "打开或切换 App";
  if (key.kind === "url") return "打开网址";
  if (key.kind === "keyboard_shortcut") return key.label || "键盘快捷键";
  if (key.kind === "agent") return "Agent 状态槽位";
  if (key.kind === "codex_pet") return "Codex 宠物";
  if (key.kind === "quota_status") return "订阅 / 限额状态";
  if (key.kind === "usage_summary") return "Token / 金额用量";
  if (key.kind === "disabled") return "暂不设定";
  return "按键";
}

/** 返回 daemon 当前 Codex 宠物诊断；尚未加载状态时返回空对象。 */
function codexPetStatus() {
  return state.status?.codex_pet || {};
}

/** 把宠物全局活动枚举转换成紧凑中文预览文案。 */
function codexPetActivityLabel(activity) {
  if (activity === "running") return "运行中";
  if (activity === "needs_input") return "等待输入";
  if (activity === "blocked") return "受阻";
  if (activity === "ready") return "已完成";
  return "闲置";
}

/** 汇总宠物选择和解析结果，供检查器展示真实 daemon 状态。 */
function codexPetResolutionLabel(pet) {
  const resolution = pet.resolution_status || pet.resolution || pet.status;
  if (resolution === "loaded" || resolution === "resolved" || resolution === "ready") return "已加载";
  if (resolution === "stale") return "使用最近一次成功素材";
  if (resolution === "builtin_unsupported" || resolution === "builtin") return "内置宠物首版暂不解析";
  if (resolution === "disabled") return "功能已关闭";
  if (resolution === "not_selected") return "Codex 尚未选择宠物";
  if (pet.last_error) return `加载失败：${pet.last_error}`;
  return "等待 daemon 解析";
}

/** 汇总宠物动画模式，并把 auto 读取失败与素材错误分开呈现。 */
function codexPetMotionLabel(pet) {
  const effective = pet.effective_motion === "reduced" ? "减少动态" : "完整动画";
  return pet.motion_error ? `${effective}（自动检测失败）` : effective;
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

const SHORTCUT_MODIFIERS = ["command", "control", "option", "shift"];
const SHORTCUT_MODIFIER_SYMBOLS = {
  command: "⌘",
  control: "⌃",
  option: "⌥",
  shift: "⇧",
};
const SHORTCUT_SPECIAL_LABELS = {
  Backquote: "`", Minus: "−", Equal: "=", BracketLeft: "[", BracketRight: "]",
  Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/",
  Enter: "↩", Escape: "Esc", Backspace: "⌫", Tab: "⇥", Space: "Space",
  Insert: "Ins", Delete: "Del", Home: "Home", End: "End", PageUp: "PgUp", PageDown: "PgDn",
  ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→",
  NumpadDecimal: "Num .", NumpadMultiply: "Num ×", NumpadAdd: "Num +",
  NumpadDivide: "Num ÷", NumpadEnter: "Num ↩", NumpadSubtract: "Num −",
  NumpadEqual: "Num =", NumLock: "Clear",
};
const SHORTCUT_KEY_CODES = [
  ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").map((letter) => `Key${letter}`),
  ..."0123456789".split("").map((digit) => `Digit${digit}`),
  "Backquote", "Minus", "Equal", "BracketLeft", "BracketRight", "Backslash",
  "Semicolon", "Quote", "Comma", "Period", "Slash", "Enter", "Escape", "Backspace",
  "Tab", "Space", "Insert", "Delete", "Home", "End", "PageUp", "PageDown",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  ...Array.from({ length: 20 }, (_, index) => `F${index + 1}`),
  ..."0123456789".split("").map((digit) => `Numpad${digit}`),
  "NumpadDecimal", "NumpadMultiply", "NumpadAdd", "NumpadDivide", "NumpadEnter",
  "NumpadSubtract", "NumpadEqual", "NumLock",
];

/** 将 W3C KeyboardEvent.code 转成硬件图标和序列列表共用的短标签。 */
function shortcutKeyCodeLabel(code) {
  if (!code) return "";
  if (/^Key[A-Z]$/.test(code)) return code.slice(-1);
  if (/^Digit[0-9]$/.test(code)) return code.slice(-1);
  if (/^Numpad[0-9]$/.test(code)) return `Num ${code.slice(-1)}`;
  return SHORTCUT_SPECIAL_LABELS[code] || code;
}

/** 返回一个步骤的 macOS 风格紧凑标签，例如 ⌘⇧P 或纯修饰键 ⇧。 */
function shortcutStepLabel(step) {
  const modifiers = SHORTCUT_MODIFIERS
    .filter((modifier) => (step?.modifiers || []).includes(modifier))
    .map((modifier) => SHORTCUT_MODIFIER_SYMBOLS[modifier])
    .join("");
  return `${modifiers}${shortcutKeyCodeLabel(step?.key)}` || "未设置";
}

/** 返回完整序列的单行摘要，供标题、choice 元信息和默认 label 使用。 */
function shortcutSummary(shortcut) {
  const steps = shortcut?.steps || [];
  return steps.length ? steps.map(shortcutStepLabel).join(" → ") : "尚未设置";
}

/** 把快捷键草稿编码成由硬件 renderer 生成的同源 PNG 地址。 */
function shortcutAutoPreviewUrl(shortcut) {
  const spec = JSON.stringify(shortcut || { steps: [] });
  const draftBackground = normalizeDisplayBackgroundColor(state.displayAppearance.background_color) || "default";
  return `/ui/shortcut-icons/auto-preview.png?spec=${encodeURIComponent(spec)}&background_color=${encodeURIComponent(draftBackground)}`;
}

/** 使用硬件 renderer 的 PNG；空序列只显示尚未配置占位。 */
function renderShortcutAutoPreview(shortcut, className = "shortcut-auto-preview") {
  if (!(shortcut?.steps || []).length) {
    return `<div class="${className} empty" aria-label="尚未设置快捷键">+</div>`;
  }
  return `<img class="${className}" src="${escapeAttr(shortcutAutoPreviewUrl(shortcut))}" alt="${escapeAttr(`${shortcutSummary(shortcut)} 自动图标`)}">`;
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

/** 识别可接收 Codex 任务状态的当前 ChatGPT.app 与历史 Codex.app 目标。 */
function isCodexDesktopAppTarget(app) {
  const bundleId = (app?.bundleId || "").trim().toLocaleLowerCase();
  if (["com.openai.chat", "com.openai.chatgpt", "com.openai.codex"].includes(bundleId)) {
    return true;
  }
  const name = (app?.name || "").trim().toLocaleLowerCase();
  const basename = (app?.path || "").trim().split(/[\\/]/).pop().toLocaleLowerCase();
  return ["chatgpt", "codex"].includes(name) && ["chatgpt.app", "codex.app"].includes(basename);
}

/** 把 daemon key binding 转为可编辑的 Web 草稿，并保留无动作宠物用途。 */
function uiKeyFromBinding(binding) {
  const base = { index: binding.index, dirty: false };
  if (binding.kind === "app") {
    return {
      ...base,
      role: "quick",
      kind: "app",
      app: appFromBinding(binding),
      ambientOverlayEnabled: binding.ambient_overlay?.kind === "codex_pet",
    };
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
  if (binding.kind === "keyboard_shortcut") {
    const icon = binding.icon || { mode: "auto", asset_id: null };
    return {
      ...base,
      role: "quick",
      kind: "keyboard_shortcut",
      label: binding.label || "",
      shortcut: structuredClone(binding.shortcut || { steps: [] }),
      shortcutIcon: {
        mode: icon.mode === "custom" ? "custom" : "auto",
        assetId: icon.asset_id || null,
      },
      shortcutIconUrl: icon.asset_id
        ? `/ui/shortcut-icons/${encodeURIComponent(icon.asset_id)}/preview-96.png`
        : "",
      shortcutIconLoading: false,
    };
  }
  if (binding.kind === "agent") {
    return { ...base, role: "agent", kind: "agent", slot: 1 };
  }
  if (binding.kind === "codex_pet") {
    return { ...base, role: "ambient", kind: "codex_pet" };
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

/** 把 Web 草稿序列化为 daemon binding；宠物键不携带 action 或 intent。 */
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
      ambient_overlay: key.ambientOverlayEnabled
        ? { kind: "codex_pet", scope: "launch_target", visibility: "task_active" }
        : null,
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
  if (key.kind === "keyboard_shortcut") {
    return {
      index: key.index,
      kind: "keyboard_shortcut",
      label: key.label || shortcutSummary(key.shortcut),
      shortcut: {
        steps: (key.shortcut?.steps || []).map((step, index, steps) => ({
          key: step.key || null,
          modifiers: [...(step.modifiers || [])],
          delay_after_ms: index === steps.length - 1 ? 0 : Number(step.delay_after_ms || 0),
        })),
      },
      icon: key.shortcutIcon?.mode === "custom"
        ? { mode: "custom", asset_id: key.shortcutIcon.assetId }
        : { mode: "auto", asset_id: null },
    };
  }
  if (key.kind === "agent") {
    return { index: key.index, kind: "agent", label: "Agent" };
  }
  if (key.kind === "codex_pet") {
    return { index: key.index, kind: "codex_pet", label: "Codex 宠物" };
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

/** 渲染硬件键位的配置预览；真实宠物帧由 daemon 的硬件 provider 提供。 */
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
  if (key.kind === "keyboard_shortcut") {
    if (key.shortcutIcon?.mode === "custom" && key.shortcutIconUrl) {
      return `<div class="shortcut-custom-stack">${renderShortcutAutoPreview(key.shortcut, "shortcut-key-preview")}<img class="shortcut-custom-icon" src="${escapeAttr(key.shortcutIconUrl)}" alt=""></div>`;
    }
    return renderShortcutAutoPreview(key.shortcut, "shortcut-key-preview");
  }
  if (key.kind === "agent") {
    const agent = agentForSlot(key.slot);
    return `<div class="agent-visual ${agentVisualClass(agent)}"></div>`;
  }
  if (key.kind === "codex_pet") {
    return `
      <div class="status-key-preview quota-status-preview">
        <span>${escapeHtml(codexPetActivityLabel(codexPetStatus().activity))}</span>
        <strong>Pet</strong>
      </div>
    `;
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

/** 渲染当前硬件 profile 的主键草稿；活动拖拽期间保持现有 DOM，避免中断手势。 */
function renderKeys() {
  if (keySwapController.isDragging()) return;
  el.keyGrid.innerHTML = state.keys
    .map((key) => {
      const selected = state.selectedSurface === "key" && key.index === state.selectedIndex ? " selected" : "";
      const dirty = key.dirty ? " dirty" : "";
      const swapEnabled = key.kind !== "unassigned" && !keyHasPendingAssetWork(key);
      const dragHint = swapEnabled ? "，可拖拽到其他按键交换操作" : "";
      return `
        <button class="deck-key${selected}${dirty}" data-key="${key.index}" data-swap-enabled="${swapEnabled}" type="button" aria-label="${escapeAttr(`Key ${key.index + 1} ${keyLabel(key)}${dragHint}`)}"${swapEnabled ? ' title="拖拽到其他按键交换操作"' : ""}>
          <span class="key-inner">${renderKeyFace(key)}</span>
          ${swapEnabled ? '<span class="key-drag-affordance" aria-hidden="true"></span>' : ""}
        </button>
      `;
    })
    .join("");

  el.keyGrid.querySelectorAll(".deck-key").forEach((button) => {
    button.addEventListener("click", () => {
      state.shortcutRecordingIndex = null;
      state.shortcutManualOpenIndex = null;
      state.shortcutPermissionDetailsOpen = false;
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
  el.petsPanelControl?.classList.toggle("selected", state.selectedSurface === "pets");
  el.petsPanelControl?.setAttribute("aria-pressed", String(state.selectedSurface === "pets"));
  el.appearanceControl?.classList.toggle("selected", state.selectedSurface === "appearance");
  el.appearanceControl?.setAttribute("aria-pressed", String(state.selectedSurface === "appearance"));
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

/** 渲染快捷键的序列编辑、录制、手动添加、权限和图标控件。 */
function renderShortcutControls(key) {
  const steps = key.shortcut?.steps || [];
  const capability = state.controlCapabilities?.keyboard_shortcuts;
  const requester = capability?.permission_requester;
  const permissionPending = state.shortcutPermissionAction !== null;
  const permissionClass = capability?.permission_granted ? " granted" : "";
  const permissionMessage = capability?.supported === false
    ? capability.message || "当前平台不支持键盘快捷键"
    : capability?.permission_granted
      ? "当前快捷键执行宿主已获得键盘事件投递权限。"
      : capability?.message || "需要 macOS 辅助功能权限才能执行";
  const permissionStatus = capability === undefined
    ? "正在检查"
    : capability.supported === false
      ? "不可用"
      : capability.permission_granted
        ? "已授权"
        : "尚未授权";
  const requesterName = requester?.display_name || "当前 agent-deckd 执行宿主";
  const requesterPath = requester?.executable_path
    ? `<code class="shortcut-permission-path" title="${escapeAttr(requester.executable_path)}">${escapeHtml(requester.executable_path)}</code>`
    : "";
  const requesterNote = requester?.note
    ? `<small class="shortcut-permission-note${requester.stable_identity ? "" : " warning"}">${escapeHtml(requester.note)}</small>`
    : "";
  const permissionFeedback = state.shortcutPermissionFeedback
    ? `<div class="shortcut-permission-feedback ${escapeAttr(state.shortcutPermissionFeedback.tone)}" role="status">${escapeHtml(state.shortcutPermissionFeedback.message)}</div>`
    : "";
  const recording = state.shortcutRecordingIndex === key.index;
  const permissionDetailsOpen = state.shortcutPermissionDetailsOpen;
  const stopWillApply = recording && state.dirty && steps.length > 0;
  const recordLabel = recording
    ? stopWillApply ? "停止并应用" : "停止录制"
    : steps.length ? "继续录制" : "开始录制";
  const recordNote = recording
    ? `录制中 · 已记录 ${steps.length} 步；可继续按键，完成后点击“${stopWillApply ? "停止并应用" : "停止录制"}”。`
    : steps.length
      ? "可继续录制或编辑步骤；“应用到硬件”与顶部“保存并应用”是同一个动作。"
      : "开始后可连续记录按键；纯修饰键请使用手动添加。";
  const stepRows = steps.length
    ? steps.map((step, index) => `
      <div class="shortcut-step" data-step="${index}">
        <div class="shortcut-step-main">
          <span class="shortcut-step-number">${index + 1}</span>
          <strong>${escapeHtml(shortcutStepLabel(step))}</strong>
          <code>${escapeHtml(step.key || "modifier-only")}</code>
        </div>
        <div class="shortcut-step-actions">
          ${index > 0 ? `<button type="button" class="mini-button" data-step-action="up" data-step-index="${index}" aria-label="上移步骤 ${index + 1}">↑</button>` : ""}
          ${index < steps.length - 1 ? `<button type="button" class="mini-button" data-step-action="down" data-step-index="${index}" aria-label="下移步骤 ${index + 1}">↓</button>` : ""}
          <button type="button" class="mini-button danger" data-step-action="delete" data-step-index="${index}" aria-label="删除步骤 ${index + 1}">×</button>
        </div>
        ${index < steps.length - 1 ? `
          <label class="shortcut-delay">释放后等待
            <input class="delay-input" data-step-delay="${index}" type="number" min="0" max="2000" step="10" value="${Number(step.delay_after_ms || 0)}">
            <span>ms</span>
          </label>` : '<div class="shortcut-delay terminal">序列结束 · 不等待</div>'}
      </div>
    `).join("")
    : '<div class="shortcut-empty">先录制一步，或用手动选择器添加按键。</div>';
  const keyOptions = [
    '<option value="">仅修饰键</option>',
    ...SHORTCUT_KEY_CODES.map((code) => `<option value="${escapeAttr(code)}">${escapeHtml(shortcutKeyCodeLabel(code))} · ${escapeHtml(code)}</option>`),
  ].join("");
  const iconPreview = key.shortcutIcon?.mode === "custom" && key.shortcutIconUrl
    ? `<div class="shortcut-custom-stack">${renderShortcutAutoPreview(key.shortcut)}<img class="shortcut-icon-preview-img" src="${escapeAttr(key.shortcutIconUrl)}" alt=""></div>`
    : renderShortcutAutoPreview(key.shortcut);

  return `
    <div class="field-group">
      <div class="field-label">名称</div>
      ${textField("shortcutLabel", "按键标签", key.label || "", shortcutSummary(key.shortcut))}
    </div>
    <div class="shortcut-permission-shell${permissionDetailsOpen ? " open" : ""}">
      <div class="shortcut-permission${permissionClass}">
        <div class="shortcut-permission-heading">
          <strong>辅助功能</strong>
          <span class="shortcut-permission-badge">${permissionStatus}</span>
        </div>
        <div class="shortcut-permission-summary-actions">
          ${capability?.supported !== false && !capability?.permission_granted && capability?.can_request_permission
            ? `<button id="requestShortcutPermission" class="ghost-button compact" type="button"${permissionPending ? " disabled" : ""}>${state.shortcutPermissionAction === "request" ? "授权中" : "去授权"}</button>`
            : ""}
          <button id="toggleShortcutPermissionDetails" class="shortcut-permission-details" type="button" aria-expanded="${permissionDetailsOpen}" aria-controls="shortcutPermissionDetails" title="悬停或点击查看授权对象与操作">详情</button>
        </div>
      </div>
      <div id="shortcutPermissionDetails" class="shortcut-permission-popover" role="group" aria-label="辅助功能权限详情">
        <small>${escapeHtml(permissionMessage)}</small>
        ${capability?.supported === false ? "" : `<small class="shortcut-permission-target">授权对象：<strong>${escapeHtml(requesterName)}</strong>；浏览器无需授权。</small>`}
        ${requesterPath}
        ${requesterNote}
        ${permissionFeedback}
        ${capability?.supported === false ? "" : `
        <div class="shortcut-permission-actions">
          ${capability?.can_open_system_settings
            ? `<button id="openShortcutAccessibilitySettings" class="ghost-button compact" type="button"${permissionPending ? " disabled" : ""}>${state.shortcutPermissionAction === "settings" ? "正在打开" : "打开辅助功能设置"}</button>`
            : ""}
          <button id="recheckShortcutPermission" class="ghost-button compact" type="button"${permissionPending ? " disabled" : ""}>${state.shortcutPermissionAction === "check" ? "正在检查" : "重新检查"}</button>
        </div>`}
      </div>
    </div>
    <div class="field-group">
      <div class="field-label">动作序列 · ${steps.length}/16</div>
      <div class="shortcut-steps">${stepRows}</div>
      <div class="shortcut-record-actions">
        <button id="recordShortcutStep" class="ghost-button${recording ? " recording" : ""}" type="button">${recordLabel}</button>
        ${!recording && steps.length ? `<button id="applyShortcut" class="primary-button shortcut-apply-button" type="button"${!state.dirty || state.saving ? " disabled" : ""} title="与顶部保存并应用相同，会保存整份设备配置">${state.saving ? "保存中" : state.dirty ? "应用到硬件" : state.awaitingHardwareApply ? "等待下发" : "已应用"}</button>` : ""}
      </div>
      <p class="control-note">${escapeHtml(recordNote)}</p>
    </div>
    <details class="shortcut-manual"${state.shortcutManualOpenIndex === key.index ? " open" : ""}>
      <summary>手动添加步骤</summary>
      <div class="shortcut-manual-body">
        <label class="text-field" for="manualShortcutKey"><span>物理按键</span>
          <select id="manualShortcutKey" class="select-input">${keyOptions}</select>
        </label>
        <div class="modifier-picker" aria-label="修饰键">
          ${SHORTCUT_MODIFIERS.map((modifier) => `
            <label><input type="checkbox" data-manual-modifier="${modifier}"><span>${SHORTCUT_MODIFIER_SYMBOLS[modifier]} ${modifier}</span></label>
          `).join("")}
        </div>
        <button id="addManualShortcutStep" class="ghost-button compact" type="button">添加步骤</button>
      </div>
    </details>
    <div class="field-group">
      <div class="field-label">默认图标</div>
      <div class="shortcut-icon-control">
        <div class="shortcut-icon-preview">${iconPreview}</div>
        <div class="shortcut-icon-actions">
          <button type="button" class="icon-mode-button${key.shortcutIcon?.mode !== "custom" ? " active" : ""}" data-shortcut-icon-mode="auto">自动生成</button>
          <button type="button" class="icon-mode-button${key.shortcutIcon?.mode === "custom" ? " active" : ""}" data-shortcut-icon-mode="custom">自定义图片</button>
          <button id="chooseShortcutIcon" class="ghost-button compact" type="button"${key.shortcutIconLoading ? " disabled" : ""}>${key.shortcutIconLoading ? "正在上传" : "选择图片"}</button>
          <input id="shortcutIconFile" class="hidden-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/x-icon,image/vnd.microsoft.icon">
        </div>
      </div>
      <div class="url-icon-status">自定义图片缺失时，硬件会自动回退到组合键图标。</div>
    </div>
  `;
}

/** 渲染当前主键的用途检查器；宠物用途只展示解析状态，不提供点击动作控件。 */
function renderKeyInspector() {
  const key = state.keys[state.selectedIndex];
  el.selectedEyebrow.textContent = `Key ${key.index + 1}`;
  el.selectedTitle.textContent = keyLabel(key);
  el.selectedSubtitle.textContent =
    key.kind === "agent"
      ? "按键只表达状态，不显示文字或详情。"
      : key.kind === "codex_pet"
        ? "跟随 Codex 当前选择的宠物；仅展示，点击无动作。"
      : key.kind === "quota_status"
        ? "按下后切换 Auto、5H、Week 的订阅状态图。"
        : key.kind === "usage_summary"
          ? "按下后切换 Day、Week、Month、All 的 Token/金额统计图。"
      : key.kind === "keyboard_shortcut"
        ? "按下后向执行开始时的前台 App 发送一个物理键、组合键或有序序列。"
      : key.kind === "disabled"
        ? "这个键暂不显示内容，也不会响应按下。"
        : "修改只更新 GUI 预览，保存并应用后才下发。";

  let details = "";
  if (key.kind === "app") {
    details =
      detailRow("App", key.app.name) +
      detailRow("路径", key.app.path) +
      detailRow("图标", "使用 App 图标") +
      (isCodexDesktopAppTarget(key.app)
        ? detailRow("任务态宠物", key.ambientOverlayEnabled ? "已开启" : "已关闭")
        : "");
  } else if (key.kind === "agent") {
    const agent = agentForSlot(key.slot);
    details =
      detailRow("槽位", `第 ${key.slot} 个 Agent`) +
      detailRow("当前", agent ? agent.status : "空槽") +
      detailRow("按下", "选择并聚焦");
  } else if (key.kind === "codex_pet") {
    const pet = codexPetStatus();
    const petName = pet.display_name || pet.name || pet.selected_avatar_id || "尚未选择";
    details =
      detailRow("当前宠物", petName) +
      detailRow("全局状态", codexPetActivityLabel(pet.activity)) +
      detailRow("素材", codexPetResolutionLabel(pet)) +
      detailRow("动画", codexPetMotionLabel(pet)) +
      detailRow("按下", "仅展示，不执行动作");
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
  } else if (key.kind === "keyboard_shortcut") {
    details =
      detailRow("序列", shortcutSummary(key.shortcut)) +
      detailRow("目标", "执行开始时的前台 App") +
      detailRow("并发", "执行中再次按下会返回忙碌");
  }

  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <div class="field-label">用途</div>
      <div class="choice-grid">
        ${choiceButton("app", "打开或切换 App", key.kind === "app" ? key.app.name : "")}
        ${choiceButton("url", "打开网址", "")}
        ${choiceButton("keyboard_shortcut", "键盘快捷键", key.kind === "keyboard_shortcut" ? shortcutSummary(key.shortcut) : "")}
        ${choiceButton("quota_status", "订阅 / 限额状态", key.kind === "quota_status" ? quotaWindowLabel(key.quotaWindow) : "")}
        ${choiceButton("usage_summary", "Token / 金额用量", key.kind === "usage_summary" ? usagePeriodLabel(key.usagePeriod) : "")}
        ${choiceButton("codex_pet", "Codex 宠物", key.kind === "codex_pet" ? codexPetActivityLabel(codexPetStatus().activity) : "仅展示")}
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
    ${key.kind === "keyboard_shortcut" ? renderShortcutControls(key) : ""}
    ${
      key.kind === "app" && isCodexDesktopAppTarget(key.app)
        ? `<div class="field-group">
            <div class="field-label">任务状态展示</div>
            <label class="switch-field" for="codexPetAmbientOverlay">
              <span><strong>任务活跃时显示宠物</strong><small>空闲时恢复 ${escapeHtml(key.app.name)} 原图标；按键动作不变。</small></span>
              <input id="codexPetAmbientOverlay" type="checkbox"${key.ambientOverlayEnabled ? " checked" : ""}>
            </label>
          </div>`
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

  document.getElementById("codexPetAmbientOverlay")?.addEventListener("change", (event) => {
    key.ambientOverlayEnabled = event.target.checked;
    markDirty(key);
    render();
  });

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

  if (key.kind === "keyboard_shortcut") {
    bindShortcutControls(key);
  }

}

/** 追加步骤并返回是否成功；达到 16 步时停止录制但保留现有草稿。 */
function appendShortcutStep(key, step) {
  const steps = key.shortcut?.steps || [];
  if (steps.length >= 16) {
    el.toast.textContent = "一个快捷键最多包含 16 个步骤";
    state.shortcutRecordingIndex = null;
    render();
    return false;
  }
  if (steps.length) steps[steps.length - 1].delay_after_ms = 100;
  steps.push({
    key: step.key || null,
    modifiers: SHORTCUT_MODIFIERS.filter((modifier) => (step.modifiers || []).includes(modifier)),
    delay_after_ms: 0,
  });
  key.shortcut = { steps };
  markDirty(key);
  render();
  return true;
}

/** 为当前快捷键 inspector 绑定序列、权限和图标控件事件。 */
function bindShortcutControls(key) {
  const manualDetails = el.inspectorBody.querySelector("details.shortcut-manual");
  manualDetails?.addEventListener("toggle", () => {
    state.shortcutManualOpenIndex = manualDetails.open ? key.index : null;
    if (manualDetails.open && state.shortcutRecordingIndex === key.index) {
      state.shortcutRecordingIndex = null;
      render();
    }
  });

  document.getElementById("shortcutLabel")?.addEventListener("input", (event) => {
    key.label = event.target.value.trimStart();
    markDirty(key);
    el.selectedTitle.textContent = keyLabel(key);
  });

  document.getElementById("recordShortcutStep")?.addEventListener("click", () => {
    if (state.shortcutRecordingIndex === key.index) {
      const shouldApply = state.dirty && (key.shortcut?.steps || []).length > 0;
      state.shortcutRecordingIndex = null;
      render();
      if (shouldApply) saveAndApply();
      return;
    }
    state.shortcutManualOpenIndex = null;
    state.shortcutRecordingIndex = key.index;
    el.toast.textContent = "录制已开始；请按下一个或多个物理按键";
    render();
  });

  document.getElementById("applyShortcut")?.addEventListener("click", saveAndApply);

  document.getElementById("addManualShortcutStep")?.addEventListener("click", () => {
    const selectedKey = document.getElementById("manualShortcutKey")?.value || null;
    const modifiers = Array.from(document.querySelectorAll("[data-manual-modifier]:checked"))
      .map((input) => input.dataset.manualModifier);
    if (!selectedKey && !modifiers.length) {
      el.toast.textContent = "请选择物理按键或至少一个修饰键";
      return;
    }
    state.shortcutRecordingIndex = null;
    appendShortcutStep(key, { key: selectedKey, modifiers });
  });

  el.inspectorBody.querySelectorAll("[data-step-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.stepIndex);
      const steps = key.shortcut?.steps || [];
      if (!Number.isInteger(index) || !steps[index]) return;
      if (button.dataset.stepAction === "delete") {
        steps.splice(index, 1);
      } else if (button.dataset.stepAction === "up" && index > 0) {
        [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]];
      } else if (button.dataset.stepAction === "down" && index < steps.length - 1) {
        [steps[index], steps[index + 1]] = [steps[index + 1], steps[index]];
      }
      steps.forEach((step, stepIndex) => {
        if (stepIndex === steps.length - 1) step.delay_after_ms = 0;
        else if (!Number.isFinite(Number(step.delay_after_ms))) step.delay_after_ms = 100;
      });
      markDirty(key);
      render();
    });
  });

  el.inspectorBody.querySelectorAll("[data-step-delay]").forEach((input) => {
    input.addEventListener("change", () => {
      const index = Number(input.dataset.stepDelay);
      const step = key.shortcut?.steps?.[index];
      if (!step) return;
      step.delay_after_ms = Math.max(0, Math.min(2000, Number(input.value) || 0));
      input.value = String(step.delay_after_ms);
      markDirty(key);
    });
  });

  el.inspectorBody.querySelectorAll("[data-shortcut-icon-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.shortcutIconMode;
      if (mode === "custom" && !key.shortcutIcon?.assetId) {
        document.getElementById("shortcutIconFile")?.click();
        return;
      }
      key.shortcutIcon = mode === "custom"
        ? { mode: "custom", assetId: key.shortcutIcon.assetId }
        : { mode: "auto", assetId: null };
      markDirty(key);
      render();
    });
  });

  const chooseIcon = document.getElementById("chooseShortcutIcon");
  const iconFile = document.getElementById("shortcutIconFile");
  if (chooseIcon && iconFile) {
    chooseIcon.addEventListener("click", () => iconFile.click());
    iconFile.addEventListener("change", () => {
      const file = iconFile.files?.[0];
      if (file) uploadShortcutIconForKey(key.index, file);
      iconFile.value = "";
    });
  }

  document.getElementById("requestShortcutPermission")?.addEventListener("click", requestShortcutPermission);
  document.getElementById("toggleShortcutPermissionDetails")?.addEventListener("click", () => {
    state.shortcutPermissionDetailsOpen = !state.shortcutPermissionDetailsOpen;
    render();
  });
  document.getElementById("openShortcutAccessibilitySettings")?.addEventListener("click", openShortcutAccessibilitySettings);
  document.getElementById("recheckShortcutPermission")?.addEventListener("click", recheckShortcutPermission);
}

/** 从显式按钮请求 macOS 键盘事件权限，并刷新 capability banner。 */
async function requestShortcutPermission() {
  state.shortcutPermissionAction = "request";
  state.shortcutPermissionFeedback = {
    tone: "info",
    message: "正在由当前 Agent 后台进程请求 macOS 权限……",
  };
  render();
  try {
    const response = await fetch("/ui/keyboard-shortcuts/request-permission", { method: "POST" });
    if (!response.ok) throw new Error(`permission ${response.status}`);
    const capability = await response.json();
    state.controlCapabilities = state.controlCapabilities || {};
    state.controlCapabilities.keyboard_shortcuts = capability;
    state.shortcutPermissionDetailsOpen = !capability.permission_granted;
    state.shortcutPermissionFeedback = capability.permission_granted
      ? { tone: "success", message: "当前快捷键执行宿主已获得权限。" }
      : { tone: "warning", message: "系统尚未授权。请打开辅助功能设置，启用刚出现的执行宿主条目，然后点“重新检查”。" };
    el.toast.textContent = state.shortcutPermissionFeedback.message;
  } catch (error) {
    state.shortcutPermissionDetailsOpen = true;
    state.shortcutPermissionFeedback = { tone: "error", message: `权限请求失败：${error.message}` };
    el.toast.textContent = state.shortcutPermissionFeedback.message;
  } finally {
    state.shortcutPermissionAction = null;
    render();
  }
}

/** 从显式按钮让 daemon 打开固定的 macOS 辅助功能设置页面。 */
async function openShortcutAccessibilitySettings() {
  state.shortcutPermissionDetailsOpen = true;
  state.shortcutPermissionAction = "settings";
  state.shortcutPermissionFeedback = { tone: "info", message: "正在打开 macOS 辅助功能设置……" };
  render();
  try {
    const response = await fetch("/ui/keyboard-shortcuts/open-accessibility-settings", { method: "POST" });
    if (!response.ok) throw new Error(`system settings ${response.status}`);
    state.shortcutPermissionFeedback = {
      tone: "info",
      message: "系统设置已打开。请授权当前执行宿主；浏览器不需要授权。完成后返回并点“重新检查”。",
    };
    el.toast.textContent = state.shortcutPermissionFeedback.message;
  } catch (error) {
    state.shortcutPermissionFeedback = { tone: "error", message: `系统设置打开失败：${error.message}` };
    el.toast.textContent = state.shortcutPermissionFeedback.message;
  } finally {
    state.shortcutPermissionAction = null;
    render();
  }
}

/** 重新读取 daemon 当前键盘事件权限，并在卡片内显示结果。 */
async function recheckShortcutPermission() {
  state.shortcutPermissionAction = "check";
  state.shortcutPermissionFeedback = { tone: "info", message: "正在重新检查当前执行宿主权限……" };
  render();
  try {
    const response = await fetch("/ui/control-capabilities", { cache: "no-store" });
    if (!response.ok) throw new Error(`control capabilities ${response.status}`);
    state.controlCapabilities = await response.json();
    const capability = state.controlCapabilities.keyboard_shortcuts;
    state.shortcutPermissionFeedback = capability?.permission_granted
      ? { tone: "success", message: "权限检查通过，可以执行快捷键。" }
      : { tone: "warning", message: "仍未检测到权限。请确认授权的是当前执行宿主，而不是浏览器。" };
    el.toast.textContent = state.shortcutPermissionFeedback.message;
  } catch (error) {
    state.shortcutPermissionFeedback = { tone: "error", message: `权限检查失败：${error.message}` };
    el.toast.textContent = state.shortcutPermissionFeedback.message;
  } finally {
    state.shortcutPermissionAction = null;
    render();
  }
}

/** 上传快捷键自定义图标并把内容寻址 asset id 写入当前 GUI 草稿。 */
async function uploadShortcutIconForKey(index, file) {
  if (file.size > 5 * 1024 * 1024) {
    el.toast.textContent = "快捷键图标不能超过 5 MiB";
    return;
  }
  const key = state.keys.find((item) => item.index === index);
  if (!key || key.kind !== "keyboard_shortcut") return;
  key.shortcutIconLoading = true;
  render();
  try {
    const dataUrl = await fileToDataUrl(file);
    const response = await fetch("/ui/shortcut-icons/upload", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ filename: file.name, data_url: dataUrl }),
    });
    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({}));
      throw new Error(errorBody.detail || `upload ${response.status}`);
    }
    const asset = await response.json();
    const current = state.keys.find((item) => item.index === index);
    if (!current || current.kind !== "keyboard_shortcut") return;
    current.shortcutIcon = { mode: "custom", assetId: asset.asset_id };
    current.shortcutIconUrl = asset.preview_url;
    current.shortcutIconLoading = false;
    markDirty(current);
    render();
  } catch (error) {
    const current = state.keys.find((item) => item.index === index);
    if (current) current.shortcutIconLoading = false;
    el.toast.textContent = `快捷键图标上传失败：${error.message}`;
    render();
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
 * 渲染 PETS 虚拟面板的集中设置；这些设置面向整个 N4 Pro 面板，不属于单个物理 Key。
 */
function renderPetsPanelInspector() {
  const settings = state.petsPanelSettings;
  const colony = state.status?.codex_pet?.panel_colony || {};
  el.selectedEyebrow.textContent = "Touch bar 设置";
  el.selectedTitle.textContent = "宠物巡游";
  el.selectedSubtitle.textContent = "这里配置 Touch bar 当前承载的 PETS 内容；背景属于独立的“显示外观”设置。";
  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <label class="text-field" for="remotePetSource"><span>远端 Agent 宠物</span>
        <select id="remotePetSource" class="select-input">
          <option value="follow_local"${settings.remote_pet_source === "follow_local" ? " selected" : ""}>跟随本地宠物设置</option>
          <option value="remote_config"${settings.remote_pet_source === "remote_config" ? " selected" : ""}>读取远端 ChatGPT 配置</option>
          <option value="builtin_random"${settings.remote_pet_source === "builtin_random" ? " selected" : ""}>稳定随机系统宠物（不读取远端）</option>
        </select>
      </label>
      <p class="control-note">“读取远端配置”只面向已启用 Connection：先通过 app-server config/read 获取名字型宠物 ID；custom 宠物仅镜像清单声明的图集到 Agent Deck 缓存，失败时才回退系统宠物。</p>
    </div>
    <div class="field-group">
      <label class="text-field" for="petPatrolSpeed"><span>巡游速度</span>
        <select id="petPatrolSpeed" class="select-input">
          <option value="slow"${settings.patrol_speed === "slow" ? " selected" : ""}>慢</option>
          <option value="medium"${settings.patrol_speed === "medium" ? " selected" : ""}>中</option>
          <option value="fast"${settings.patrol_speed === "fast" ? " selected" : ""}>快</option>
        </select>
      </label>
      <p class="control-note">档位只决定基础节奏；每只宠物仍会持续做细微、平滑且不同步的速度变化。</p>
    </div>
    <div class="field-group"><div class="field-label">当前场景</div>
      ${detailRow("活动宠物", String(colony.actor_count || 0))}
      ${detailRow("远端宠物", String(colony.remote_actor_count || 0))}
      ${detailRow("可渲染", String(colony.renderable_actor_count || 0))}
      ${detailRow("当前来源策略", settings.remote_pet_source === "remote_config" ? "读取远端配置" : settings.remote_pet_source === "follow_local" ? "跟随本地" : "稳定随机（未读取远端）")}
      ${detailRow("设置状态", state.petsPanelSettingsSource === "persisted" ? "已保存" : "使用 daemon 默认配置")}
      ${detailRow("远端配置可用", `${colony.remote_config_available_host_count || 0} / ${colony.remote_config_host_count || 0} 台`)}
      ${detailRow("远端 custom 缓存", `${colony.remote_custom_asset_count || 0} 个可用${colony.remote_custom_stale_count ? ` · ${colony.remote_custom_stale_count} 个陈旧回退` : ""}`)}
      ${detailRow("素材目录", `${colony.builtin_pet_count || 0} 个系统宠物`)}
    </div>
  `;
  document.getElementById("remotePetSource")?.addEventListener("change", (event) => {
    settings.remote_pet_source = event.target.value;
    state.dirty = true;
    render();
  });
  document.getElementById("petPatrolSpeed")?.addEventListener("change", (event) => {
    settings.patrol_speed = event.target.value;
    state.dirty = true;
    render();
  });
}

/** 把用户输入规范化为后端接受的 #RRGGBB；非法值返回 null。 */
function normalizeDisplayBackgroundColor(value) {
  const candidate = String(value || "").trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(candidate) ? candidate : null;
}

/** 由背景亮度推导预览前景色，模拟硬件 renderer 的自动对比度策略。 */
function displayPreviewForeground(background) {
  const hex = normalizeDisplayBackgroundColor(background) || "#0B0E12";
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
  const luminance = (0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]) / 255;
  return luminance > 0.56 ? "#111820" : "#F2F7FA";
}

/** 更新设备总览中的 Key 与 Touch bar 草稿色；不写 daemon、不触发硬件下发。 */
function renderDisplayAppearancePreview() {
  const custom = normalizeDisplayBackgroundColor(state.displayAppearance.background_color);
  const background = custom || "#0B0E12";
  const foreground = displayPreviewForeground(background);
  el.devicePreview?.style.setProperty("--display-preview-background", background);
  el.devicePreview?.style.setProperty("--display-preview-foreground", foreground);
  el.devicePreview?.classList.toggle("custom-display-background", custom !== null);
}

/** 标记独立显示外观草稿有变化，并仅刷新本地双表面预览。 */
function markDisplayAppearanceDirty(backgroundColor) {
  state.displayAppearance.background_color = backgroundColor;
  state.dirty = true;
  renderDisplayAppearancePreview();
  renderSyncState();
}

/** 渲染不隶属于 PETS、Key 或旋钮的全局显示外观设置。 */
function renderDisplayAppearanceInspector() {
  const customColor = normalizeDisplayBackgroundColor(state.displayAppearance.background_color);
  const isCustom = customColor !== null;
  const previewColor = customColor || "#0B0E12";
  const previewForeground = displayPreviewForeground(previewColor);
  el.selectedEyebrow.textContent = "显示外观";
  el.selectedTitle.textContent = "硬件内容背景";
  el.selectedSubtitle.textContent = "统一影响常规 App、自定义按键、状态、宠物和 Touch bar；修改先停留在预览，保存后才应用。";
  el.inspectorBody.innerHTML = `
    <div class="field-group">
      <div class="field-label">背景模式</div>
      <div class="appearance-mode-grid" role="radiogroup" aria-label="背景模式">
        <button class="choice-button${isCustom ? "" : " active"}" type="button" data-appearance-mode="default" role="radio" aria-checked="${String(!isCustom)}">
          <span class="choice-title">不设定</span><span class="choice-meta">沿用各内容原效果</span>
        </button>
        <button class="choice-button${isCustom ? " active" : ""}" type="button" data-appearance-mode="custom" role="radio" aria-checked="${String(isCustom)}">
          <span class="choice-title">自定义颜色</span><span class="choice-meta">跨表面统一</span>
        </button>
      </div>
      <p class="control-note">“不设定”不是黑色，而是不覆盖 renderer 现有背景，因此默认画面保持完全兼容。</p>
    </div>
    ${isCustom ? `
      <div class="field-group">
        <div class="field-label">背景颜色</div>
        <div class="color-setting">
          <input id="displayBackgroundPicker" class="color-input" type="color" value="${escapeAttr(customColor)}" aria-label="选择显示背景色">
          <input id="displayBackgroundText" class="text-input" type="text" value="${escapeAttr(customColor)}" maxlength="7" pattern="#[0-9A-Fa-f]{6}" aria-label="显示背景色十六进制值">
        </div>
      </div>
    ` : ""}
    <div class="field-group">
      <div class="field-label">双表面预览</div>
      <div class="appearance-preview-pair" style="--appearance-preview-bg:${escapeAttr(previewColor)};--appearance-preview-fg:${escapeAttr(previewForeground)}">
        <div class="appearance-key-preview"><span>A</span><small>App</small></div>
        <div class="appearance-panel-preview"><strong>AGENT DECK</strong><span>Touch bar</span></div>
      </div>
      <p class="control-note">${isCustom ? `预览 ${escapeHtml(customColor)}；文字与辅助色会自动选择可读对比度。` : "当前不覆盖背景，Key 与 Touch bar 分别沿用已有默认画面。"}</p>
    </div>
    <div class="field-group">
      ${detailRow("作用范围", "所有 Key 内容 + Touch bar")}
      ${detailRow("设置状态", state.displayAppearanceSource === "persisted" ? "已保存" : "使用默认")}
      ${detailRow("应用 revision", String(state.displayAppearanceRevision))}
    </div>
  `;
  el.inspectorBody.querySelectorAll("[data-appearance-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextColor = button.dataset.appearanceMode === "custom"
        ? customColor || "#203040"
        : null;
      markDisplayAppearanceDirty(nextColor);
      render();
    });
  });
  document.getElementById("displayBackgroundPicker")?.addEventListener("input", (event) => {
    const nextColor = normalizeDisplayBackgroundColor(event.target.value);
    if (nextColor === null) return;
    markDisplayAppearanceDirty(nextColor);
    const textInput = document.getElementById("displayBackgroundText");
    if (textInput) textInput.value = nextColor;
    const pair = el.inspectorBody.querySelector(".appearance-preview-pair");
    pair?.style.setProperty("--appearance-preview-bg", nextColor);
    pair?.style.setProperty("--appearance-preview-fg", displayPreviewForeground(nextColor));
  });
  document.getElementById("displayBackgroundText")?.addEventListener("change", (event) => {
    const nextColor = normalizeDisplayBackgroundColor(event.target.value);
    if (nextColor === null) {
      el.toast.textContent = "背景颜色必须是 #RRGGBB";
      event.target.focus();
      return;
    }
    markDisplayAppearanceDirty(nextColor);
    render();
  });
}

/**
 * 根据当前设备预览选中的表面渲染 key、旋钮或独立灯光设置检查器。
 */
function renderInspector() {
  if (state.selectedSurface === "appearance") {
    renderDisplayAppearanceInspector();
    return;
  }
  if (state.selectedSurface === "pets") {
    renderPetsPanelInspector();
    return;
  }
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

/** 在不改变两个物理位置 index 的前提下交换完整操作定义，并标记草稿待保存。 */
function swapKeyOperations(sourceIndex, targetIndex) {
  if (sourceIndex === targetIndex) return;
  const sourcePosition = state.keys.findIndex((key) => key.index === sourceIndex);
  const targetPosition = state.keys.findIndex((key) => key.index === targetIndex);
  if (sourcePosition < 0 || targetPosition < 0) return;
  const sourceKey = state.keys[sourcePosition];
  const targetKey = state.keys[targetPosition];
  if (sourceKey.kind === "unassigned") return;

  state.keys[sourcePosition] = {
    ...structuredClone(targetKey),
    index: sourceIndex,
    dirty: true,
  };
  state.keys[targetPosition] = {
    ...structuredClone(sourceKey),
    index: targetIndex,
    dirty: true,
  };
  renumberAgentSlots();
  state.shortcutRecordingIndex = null;
  state.shortcutManualOpenIndex = null;
  state.shortcutPermissionDetailsOpen = false;
  state.selectedIndex = targetIndex;
  state.selectedSurface = "key";
  state.dirty = true;
  render();
  flashSwappedKeys(sourceIndex, targetIndex);
  showTransientToast(`已交换 Key ${sourceIndex + 1} 与 Key ${targetIndex + 1}，保存并应用后下发`, 2800);
}

/** 为刚完成交换的两个物理键播放一次短促确认动画，不进入持久状态。 */
function flashSwappedKeys(sourceIndex, targetIndex) {
  window.requestAnimationFrame(() => {
    const buttons = [sourceIndex, targetIndex]
      .map((index) => el.keyGrid.querySelector(`.deck-key[data-key="${index}"]`))
      .filter(Boolean);
    buttons.forEach((button) => button.classList.add("surface-swap-complete"));
    window.setTimeout(() => {
      buttons.forEach((button) => button.classList.remove("surface-swap-complete"));
    }, 620);
  });
}

/** 展示短暂底部提示；只清理由本次调用写入且尚未被新消息替换的内容。 */
function showTransientToast(message, durationMs) {
  el.toast.textContent = message;
  window.setTimeout(() => {
    if (el.toast.textContent === message) el.toast.textContent = "";
  }, durationMs);
}

/** 切换当前键用途；Codex 宠物只写入 ambient 展示类型，不附加点击动作。 */
function updateKeyKind(kind) {
  const key = state.keys[state.selectedIndex];
  state.shortcutRecordingIndex = null;
  state.shortcutManualOpenIndex = null;
  state.shortcutPermissionDetailsOpen = false;
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
  } else if (kind === "keyboard_shortcut") {
    Object.assign(key, {
      role: "quick",
      kind: "keyboard_shortcut",
      label: key.kind === "keyboard_shortcut" ? key.label || "" : "",
      shortcut: key.kind === "keyboard_shortcut" ? key.shortcut || { steps: [] } : { steps: [] },
      shortcutIcon: key.kind === "keyboard_shortcut"
        ? key.shortcutIcon || { mode: "auto", assetId: null }
        : { mode: "auto", assetId: null },
      shortcutIconUrl: key.kind === "keyboard_shortcut" ? key.shortcutIconUrl || "" : "",
      shortcutIconLoading: false,
    });
  } else if (kind === "agent") {
    const agentCountBefore = state.keys.filter((item) => item.kind === "agent" && item.index < key.index).length;
    Object.assign(key, { role: "agent", kind: "agent", slot: agentCountBefore + 1 });
  } else if (kind === "codex_pet") {
    Object.assign(key, { role: "ambient", kind: "codex_pet" });
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
  renumberAgentSlots();
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
      key.ambientOverlayEnabled = isCodexDesktopAppTarget(app)
        ? Boolean(key.ambientOverlayEnabled)
        : false;
      renumberAgentSlots();
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
  renderDisplayAppearancePreview();
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

/** 从 daemon 读取 N4 Pro PETS 面板的持久化设置。 */
async function loadPetsPanelSettings() {
  try {
    const response = await fetch("/ui/pets-panel-settings", { cache: "no-store" });
    if (!response.ok) throw new Error(`PETS settings ${response.status}`);
    const body = await response.json();
    state.petsPanelSettings = structuredClone(body.settings);
    state.petsPanelSettingsSource = body.source || "runtime";
    render();
  } catch (error) {
    el.toast.textContent = `PETS 设置读取失败：${error.message}`;
  }
}

/** 从独立端点读取已应用显示外观，初始化本地草稿与 revision。 */
async function loadDisplayAppearance() {
  try {
    const response = await fetch("/ui/display-appearance", { cache: "no-store" });
    if (!response.ok) throw new Error(`display appearance ${response.status}`);
    const body = await response.json();
    state.displayAppearance = structuredClone(body.settings || { background_color: null });
    state.displayAppearanceSource = body.source || "default";
    state.displayAppearanceRevision = Number(body.revision || 0);
    render();
  } catch (error) {
    el.toast.textContent = `显示外观读取失败：${error.message}`;
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
  state.shortcutRecordingIndex = null;
  const validationError = validateShortcutDrafts();
  if (validationError) {
    el.toast.textContent = validationError;
    render();
    return;
  }
  const saveStartedAt = Date.now();
  state.saving = true;
  render();
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
        pets_panel_settings: state.petsPanelSettings,
        display_appearance: state.displayAppearance,
      }),
    });
    if (!response.ok) throw new Error(`save ${response.status}`);
    const body = await response.json();
    applyKeyLayoutResponse(body.key_layout);
    applyRotaryLayoutResponse(body.rotary_layout);
    if (body.pets_panel_settings?.settings) {
      state.petsPanelSettings = structuredClone(body.pets_panel_settings.settings);
      state.petsPanelSettingsSource = body.pets_panel_settings.source || "runtime";
    }
    if (body.display_appearance?.settings) {
      state.displayAppearance = structuredClone(body.display_appearance.settings);
      state.displayAppearanceSource = body.display_appearance.source || "runtime";
      state.displayAppearanceRevision = Number(body.display_appearance.revision || 0);
    }
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

/** 在发送完整配置前校验快捷键步骤数、时长和自定义图标引用。 */
function validateShortcutDrafts() {
  for (const key of state.keys) {
    if (key.kind !== "keyboard_shortcut") continue;
    const steps = key.shortcut?.steps || [];
    if (!steps.length) return `Key ${key.index + 1} 还没有快捷键步骤`;
    if (steps.length > 16) return `Key ${key.index + 1} 超过 16 个步骤`;
    const duration = steps.length * 20 + steps.reduce((sum, step, index) => {
      const delay = index === steps.length - 1 ? 0 : Number(step.delay_after_ms || 0);
      return sum + delay;
    }, 0);
    if (steps.some((step) => Number(step.delay_after_ms || 0) < 0 || Number(step.delay_after_ms || 0) > 2000)) {
      return `Key ${key.index + 1} 的步骤间隔必须在 0–2000 ms`;
    }
    if (duration > 10000) return `Key ${key.index + 1} 的序列总时长不能超过 10 秒`;
    if (key.shortcutIcon?.mode === "custom" && !key.shortcutIcon.assetId) {
      return `Key ${key.index + 1} 尚未上传自定义图标`;
    }
  }
  return "";
}

window.addEventListener("keydown", (event) => {
  if (state.shortcutRecordingIndex === null) {
    if (event.key === "Escape" && state.shortcutPermissionDetailsOpen) {
      state.shortcutPermissionDetailsOpen = false;
      render();
    }
    return;
  }
  event.preventDefault();
  event.stopImmediatePropagation();
  if (event.repeat) return;
  if (["MetaLeft", "MetaRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "ShiftLeft", "ShiftRight"].includes(event.code)) {
    el.toast.textContent = "继续按主键；纯修饰键步骤可在手动选择器中添加";
    return;
  }
  if (!SHORTCUT_KEY_CODES.includes(event.code)) {
    el.toast.textContent = `暂不支持物理键 ${event.code || event.key}`;
    return;
  }
  const key = state.keys.find((item) => item.index === state.shortcutRecordingIndex);
  if (!key || key.kind !== "keyboard_shortcut") {
    state.shortcutRecordingIndex = null;
    return;
  }
  const modifiers = [];
  if (event.metaKey) modifiers.push("command");
  if (event.ctrlKey) modifiers.push("control");
  if (event.altKey) modifiers.push("option");
  if (event.shiftKey) modifiers.push("shift");
  if (appendShortcutStep(key, { key: event.code, modifiers })) {
    el.toast.textContent = `已录制 ${shortcutStepLabel({ key: event.code, modifiers })} · 共 ${key.shortcut.steps.length} 步`;
  }
}, { capture: true });

document.addEventListener("click", (event) => {
  if (!state.shortcutPermissionDetailsOpen) return;
  if (event.target.closest?.(".shortcut-permission-shell")) return;
  state.shortcutPermissionDetailsOpen = false;
  render();
});

el.saveButton.addEventListener("click", saveAndApply);
el.themeToggle?.addEventListener("click", () => {
  applyTheme(state.theme === "light" ? "dark" : "light", { persist: true });
});
el.lightingControl?.addEventListener("click", () => {
  state.selectedSurface = "lighting";
  render();
});
el.petsPanelControl?.addEventListener("click", () => {
  state.selectedSurface = "pets";
  render();
});
el.appearanceControl?.addEventListener("click", () => {
  state.selectedSurface = "appearance";
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
  await loadPetsPanelSettings();
  await loadDisplayAppearance();
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
