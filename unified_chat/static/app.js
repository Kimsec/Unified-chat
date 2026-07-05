const state = {
  messages: [],
  statuses: new Map(),
  filters: {
    twitch: true,
    youtube: true,
    kick: true,
  },
  hypeTrain: null,
};
const MAX_VISIBLE_MESSAGES = 200;

const feedEl = document.getElementById("feed");
const statusGridEl = document.getElementById("status-grid");
const replyFormEl = document.getElementById("reply-form");
const replyStatusEl = document.getElementById("reply-status");
const refreshButtonEl = document.getElementById("refresh-status");

const PLATFORM_NAMES = { twitch: "Twitch", youtube: "YouTube", kick: "Kick" };
const PLATFORM_SVGS = {
  twitch: `<svg viewBox="0 0 256 268" aria-hidden="true"><path fill="#9146ff" d="M17.46 0L0 46.56v185.21h63.14V268h46.87l36.49-36.23h54.91L256 177.68V0H17.46zm23.07 23.07H232.9v143.14l-41.47 41.47h-69.15L85.79 244.2v-36.52H40.53V23.07zm69.15 104.55h23.07V69.26h-23.07v58.36zm63.14 0h23.07V69.26h-23.07v58.36z"/></svg>`,
  youtube: `<svg viewBox="0 0 576 512" aria-hidden="true"><path fill="#ff0000" d="M549.66 124.63a68.28 68.28 0 0 0-48.05-48.28C458.78 64 288 64 288 64S117.22 64 74.39 76.35a68.28 68.28 0 0 0-48.05 48.28C14.48 167.83 14.48 256 14.48 256s0 88.17 11.86 131.37a68.28 68.28 0 0 0 48.05 48.28C117.22 448 288 448 288 448s170.78 0 213.61-12.35a68.28 68.28 0 0 0 48.05-48.28C561.52 344.17 561.52 256 561.52 256s0-88.17-11.86-131.37zM232.15 337.28V174.72L374.86 256l-142.71 81.28z"/></svg>`,
  kick: `<img src="/static/kick-logo.ico" aria-hidden="true">`,
};
const PLATFORM_NAMES_STORAGE_KEY = "showPlatformNames";
const platformNamesChannel = typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel("unified-chat-platform-preferences")
  : null;
const platformNamesOverride = readPlatformNamesOverride();
let showPlatformNames = platformNamesOverride ?? readPlatformNamesPreference();

function readPlatformNamesOverride() {
  const rawValue = window.UNIFIED_CHAT_CONFIG?.platformNamesOverride;
  if (rawValue == null || rawValue === "") return null;
  const normalized = String(rawValue).trim().toLowerCase();
  if (["0", "false", "off", "no"].includes(normalized)) return false;
  if (["1", "true", "on", "yes"].includes(normalized)) return true;
  return null;
}

function readPlatformNamesPreference() {
  try {
    return window.localStorage.getItem(PLATFORM_NAMES_STORAGE_KEY) !== "false";
  } catch (_) {
    return true;
  }
}

function updatePlatformNamesPreference(nextValue, { broadcast = false } = {}) {
  showPlatformNames = Boolean(nextValue);
  if (platformNamesOverride === null) {
    try {
      window.localStorage.setItem(PLATFORM_NAMES_STORAGE_KEY, String(showPlatformNames));
    } catch (_) {}
  }
  if (toggleNames) {
    toggleNames.classList.toggle("active", showPlatformNames);
  }
  renderStatuses();
  renderMessages();
  if (broadcast && platformNamesChannel && platformNamesOverride === null) {
    platformNamesChannel.postMessage({ type: "showPlatformNames", value: showPlatformNames });
  }
}

function platformMarkup(platform) {
  const name = showPlatformNames ? `<span class="platform-name">${PLATFORM_NAMES[platform]}</span>` : "";
  return `<span class="platform-pill ${platform}">${PLATFORM_SVGS[platform]}${name}</span>`;
}


const AUTHOR_COLOR_BG = "#09111f";
const MIN_AUTHOR_CONTRAST = 4.0;

function parseHexColor(value) {
  if (!value) return null;
  const match = /^#?([0-9a-f]{6})$/i.exec(String(value).trim());
  if (!match) return null;
  const hex = `#${match[1].toLowerCase()}`;
  const n = parseInt(match[1], 16);
  return {
    r: (n >> 16) & 0xff,
    g: (n >> 8) & 0xff,
    b: n & 0xff,
    hex,
  };
}

const AUTHOR_COLOR_BG_RGB = parseHexColor(AUTHOR_COLOR_BG);

function srgbChannelToLinear(channel) {
  const value = channel / 255;
  return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(rgb) {
  return (
    0.2126 * srgbChannelToLinear(rgb.r) +
    0.7152 * srgbChannelToLinear(rgb.g) +
    0.0722 * srgbChannelToLinear(rgb.b)
  );
}

function contrastRatio(fg, bg) {
  const fgLum = relativeLuminance(fg);
  const bgLum = relativeLuminance(bg);
  const lighter = Math.max(fgLum, bgLum);
  const darker = Math.min(fgLum, bgLum);
  return (lighter + 0.05) / (darker + 0.05);
}

function mixTowardWhite(rgb, amount) {
  return {
    r: Math.round(rgb.r + (255 - rgb.r) * amount),
    g: Math.round(rgb.g + (255 - rgb.g) * amount),
    b: Math.round(rgb.b + (255 - rgb.b) * amount),
  };
}

function rgbToHex(rgb) {
  return `#${((rgb.r << 16) | (rgb.g << 8) | rgb.b).toString(16).padStart(6, "0")}`;
}

function ensureReadableColor(value) {
  const color = parseHexColor(value);
  if (!color || !AUTHOR_COLOR_BG_RGB) return "";
  if (contrastRatio(color, AUTHOR_COLOR_BG_RGB) >= MIN_AUTHOR_CONTRAST) return color.hex;

  let low = 0;
  let high = 1;
  for (let i = 0; i < 8; i += 1) {
    const mid = (low + high) / 2;
    const candidate = mixTowardWhite(color, mid);
    if (contrastRatio(candidate, AUTHOR_COLOR_BG_RGB) >= MIN_AUTHOR_CONTRAST) {
      high = mid;
    } else {
      low = mid;
    }
  }
  return rgbToHex(mixTowardWhite(color, high));
}

const URL_REGEX = /\bhttps?:\/\/[^\s<>"']+/g;
const TRAILING_PUNCT = /[.,;:!?)\]}]+$/;

function linkifyText(text) {
  if (!text) return "";
  let result = "";
  let last = 0;
  URL_REGEX.lastIndex = 0;
  let match;
  while ((match = URL_REGEX.exec(text)) !== null) {
    let url = match[0];
    let trailing = "";
    const trail = url.match(TRAILING_PUNCT);
    if (trail) {
      trailing = trail[0];
      url = url.slice(0, -trailing.length);
    }
    result += escapeHtml(text.slice(last, match.index));
    const escaped = escapeHtml(url);
    result += `<a href="${escaped}" target="_blank" rel="noopener noreferrer" class="message-link">${escaped}</a>`;
    result += escapeHtml(trailing);
    last = match.index + match[0].length;
  }
  result += escapeHtml(text.slice(last));
  return result;
}

const EMOTE_IMAGE_URLS = {
  twitch: (id) => `https://static-cdn.jtvnw.net/emoticons/v2/${id}/default/dark/1.0`,
  kick: (id) => `https://files.kick.com/emotes/${id}/fullsize`,
};

function renderMessageText(text, emotes, platform) {
  if (!emotes || !emotes.length) return linkifyText(text);
  const emoteUrl = EMOTE_IMAGE_URLS[platform] || EMOTE_IMAGE_URLS.twitch;
  const sorted = [...emotes].sort((a, b) => a.begin - b.begin);
  let result = "";
  let cursor = 0;
  for (const emote of sorted) {
    if (emote.begin > cursor) {
      result += linkifyText(text.slice(cursor, emote.begin));
    }
    result += `<img class="emote" src="${emoteUrl(encodeURIComponent(emote.id))}" alt="${escapeHtml(emote.text)}" title="${escapeHtml(emote.text)}">`;
    cursor = emote.end;
  }
  if (cursor < text.length) {
    result += linkifyText(text.slice(cursor));
  }
  return result;
}

function formatTime(isoString) {
  try {
    return new Date(isoString).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch (_) {
    return "";
  }
}

function normalizeMessages(messages) {
  const map = new Map();
  for (const message of messages) {
    map.set(message.id, message);
  }
  state.messages = Array.from(map.values())
    .sort((a, b) => new Date(a.sent_at) - new Date(b.sent_at))
    .slice(-MAX_VISIBLE_MESSAGES);
}

function renderMessages() {
  const visibleMessages = state.messages.filter((message) => state.filters[message.platform] !== false);

  if (!visibleMessages.length) {
    feedEl.innerHTML = `<div class="empty-state">No messages yet.</div>`;
    return;
  }

  const wasNearBottom = isNearBottom();

  feedEl.innerHTML = visibleMessages.map((message) => {
    const messageClass = message.deleted_at ? "message-card deleted" : "message-card";
    const readableColor = ensureReadableColor(message.author_color);
    const authorStyle = readableColor ? `style="color:${readableColor}"` : "";
    const sourceBroadcaster = message.raw_payload?.payload?.event?.source_broadcaster || null;
    const isSystemNotice = message.message_kind === "system";
    const sourceAvatar = message.platform === "twitch" && message.avatar_url
      ? `<img class="source-streamer-avatar" src="${escapeHtml(message.avatar_url)}" alt="" title="${escapeHtml(sourceBroadcaster?.name || sourceBroadcaster?.login || "Shared chat source")}" aria-hidden="true">`
      : "";

    if (isSystemNotice) {
      return `
        <article class="${messageClass} system-notice" data-platform="${message.platform}">
          <span class="message-topline"><span class="message-time">${formatTime(message.sent_at)}</span> ${platformMarkup(message.platform)}${sourceAvatar}<span class="message-text system-notice-text">${renderMessageText(message.text, message.emotes, message.platform)}</span></span>
        </article>
      `;
    }

    const canModerate = message.platform === "twitch"
      && message.author_id
      && message.author_id !== message.channel_id;
    const modAttrs = canModerate
      ? ` data-mod-user-id="${escapeHtml(message.author_id)}" data-mod-user-name="${escapeHtml(message.author_display_name)}"`
      : "";

    return `
      <article class="${messageClass}" data-platform="${message.platform}">
        <span class="message-topline"><span class="message-time">${formatTime(message.sent_at)}</span> ${platformMarkup(message.platform)}${sourceAvatar}<span class="author-name" ${authorStyle}${modAttrs}>${escapeHtml(message.author_display_name)}:</span> <span class="message-text">${renderMessageText(message.text, message.emotes, message.platform)}</span></span>
      </article>
    `;
  }).join("");

  requestAnimationFrame(() => {
    if (wasNearBottom) {
      modScrollCloseSuppressedUntil = performance.now() + 200;
      feedEl.scrollTop = feedEl.scrollHeight;
    }
  });
}

// Auto-scroll from renderMessages() must not close the mod panel; only user scrolls do.
let modScrollCloseSuppressedUntil = 0;

function isNearBottom() {
  return feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 100;
}

const scrollBottomBtn = document.getElementById("scroll-bottom");

feedEl.addEventListener("scroll", () => {
  if (scrollBottomBtn) {
    scrollBottomBtn.classList.toggle("hidden", isNearBottom());
  }
});

if (scrollBottomBtn) {
  scrollBottomBtn.addEventListener("click", () => {
    feedEl.scrollTop = feedEl.scrollHeight;
  });
}

function dotClassForStatus(status) {
  if (status.connected || status.state === "connected") return "ok";
  if (["starting", "connecting", "subscribing", "rate_limited", "reconnecting", "waiting_for_token", "idle"].includes(status.state)) {
    return "warn";
  }
  return "error";
}

function renderStatuses() {
  if (!statusGridEl) return;
  const ordered = ["twitch", "youtube", "kick"].map((platform) => state.statuses.get(platform) || {
    platform,
    state: "starting",
    detail: "Waiting for data",
    connected: false,
    auth_ready: false,
  });

  statusGridEl.innerHTML = ordered.map((status) => `
    <article class="status-card">
      <div class="status-head">
        ${platformMarkup(status.platform)}
        <span class="status-dot ${dotClassForStatus(status)}" aria-hidden="true"></span>
      </div>
      <div class="status-meta">
        <div><strong>${escapeHtml(status.state)}</strong></div>
        <div>${escapeHtml(status.detail || "No detail yet")}</div>
        <div>Auth: ${status.auth_ready ? "ready" : "not ready"}</div>
      </div>
    </article>
  `).join("");
}

function applyBootstrap(payload) {
  if (Array.isArray(payload.messages)) {
    normalizeMessages(payload.messages);
  }
  if (Array.isArray(payload.statuses)) {
    for (const status of payload.statuses) {
      state.statuses.set(status.platform, status);
    }
  }
  renderStatuses();
  renderMessages();
  handleHypeTrain(payload.hype_train ?? null);
}

function markMessageDeleted(payload) {
  const message = state.messages.find((candidate) =>
    candidate.platform === payload.platform &&
    candidate.platform_message_id === payload.platform_message_id
  );
  if (!message) return;
  message.deleted_at = payload.deleted_at;
  renderMessages();
}

function handleSocketPayload(payload) {
  if (!payload || typeof payload !== "object") return;
  if (payload.type === "bootstrap") {
    applyBootstrap(payload);
    return;
  }
  if (payload.type === "message" && payload.message) {
    normalizeMessages([...state.messages, payload.message]);
    renderMessages();
    return;
  }
  if (payload.type === "status" && payload.status) {
    state.statuses.set(payload.status.platform, payload.status);
    renderStatuses();
    return;
  }
  if (payload.type === "message_deleted") {
    markMessageDeleted(payload);
    return;
  }
  if (payload.type === "hype_train") {
    handleHypeTrain(payload);
  }
}

let hypeTrainEndTimer = null;

function clearHypeTrainTimer() {
  if (hypeTrainEndTimer) {
    clearTimeout(hypeTrainEndTimer);
    hypeTrainEndTimer = null;
  }
}

function resetHypeTrainBar() {
  state.hypeTrain = null;
  clearHypeTrainTimer();
  const bar = document.getElementById("hype-train-bar");
  if (!bar) return;
  const levelEl = document.getElementById("ht-level");
  const progressEl = document.getElementById("ht-progress-text");
  const fillEl = document.getElementById("ht-fill");
  if (levelEl) levelEl.textContent = "1";
  if (progressEl) progressEl.textContent = "";
  if (fillEl) fillEl.style.width = "0%";
  bar.classList.add("hidden");
  bar.setAttribute("aria-hidden", "true");
  delete bar.dataset.phase;
}

function scheduleHypeTrainHide(delayMs = 5000) {
  clearHypeTrainTimer();
  const safeDelay = Math.max(Number(delayMs) || 0, 0);
  hypeTrainEndTimer = window.setTimeout(() => {
    resetHypeTrainBar();
  }, safeDelay);
}

function handleHypeTrain(data) {
  if (!data) {
    resetHypeTrainBar();
    return;
  }

  clearHypeTrainTimer();
  state.hypeTrain = data;

  if (data.phase === "end") {
    renderHypeTrain(data);
    scheduleHypeTrainHide(data.hide_after_ms ?? 5000);
    return;
  }

  renderHypeTrain(data);
}

function renderHypeTrain(data) {
  const bar = document.getElementById("hype-train-bar");
  if (!bar) return;
  bar.classList.remove("hidden");
  bar.setAttribute("aria-hidden", "false");
  bar.dataset.phase = data.phase || "progress";
  const levelEl = document.getElementById("ht-level");
  const progressEl = document.getElementById("ht-progress-text");
  const fillEl = document.getElementById("ht-fill");
  if (levelEl) levelEl.textContent = data.level || 1;
  const progress = data.progress || 0;
  const goal = data.goal > 0 ? data.goal : 1;
  const pct = Math.min(Math.round((progress / goal) * 100), 100);
  if (data.phase === "end") {
    if (progressEl) progressEl.textContent = `Ended (${pct}%)`;
  } else {
    if (progressEl) progressEl.textContent = `${pct}%`;
  }
  if (fillEl) fillEl.style.width = `${pct}%`;
}

async function fetchBootstrap() {
  const response = await fetch("/api/messages");
  if (response.status === 401) {
    window.location.href = "/login";
    return;
  }
  const payload = await response.json();
  applyBootstrap(payload);
}

let activeSocket = null;

function connectSocket() {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${window.location.host}/ws/chat`);
  activeSocket = socket;
  socket.addEventListener("message", (event) => {
    try {
      handleSocketPayload(JSON.parse(event.data));
    } catch (_) {}
  });
  socket.addEventListener("open", () => {
    socket.send("ready");
  });
  socket.addEventListener("close", () => {
    if (socket !== activeSocket) return; // superseded by a newer connection
    window.setTimeout(() => {
      if (socket === activeSocket) connectSocket();
    }, 3000);
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!activeSocket || activeSocket.readyState === WebSocket.CLOSING || activeSocket.readyState === WebSocket.CLOSED) {
    connectSocket();
  }
});

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    const platform = button.dataset.platform;
    state.filters[platform] = !state.filters[platform];
    button.classList.toggle("active", state.filters[platform]);
    renderMessages();
  });
});

window.addEventListener("resize", () => {
  feedEl.scrollTop = feedEl.scrollHeight;
});

const clearBtn = document.getElementById("clear-messages");
if (clearBtn) {
  clearBtn.addEventListener("click", async () => {
    if (!confirm("Clear all messages?")) return;
    await fetch("/api/clear-messages", { method: "POST" });
    state.messages = [];
    renderMessages();
  });
}

const popoutBtn = document.getElementById("popout-chat");
if (popoutBtn) {
  popoutBtn.addEventListener("click", () => {
    window.open("/popout", "unified-chat-popout", "width=500,height=800,resizable=yes,scrollbars=no");
  });
}

const toggleNames = document.getElementById("toggle-platform-names");
if (toggleNames) {
  toggleNames.classList.toggle("active", showPlatformNames);
  toggleNames.disabled = platformNamesOverride !== null;
  toggleNames.addEventListener("click", () => {
    updatePlatformNamesPreference(!showPlatformNames, { broadcast: true });
  });
}

window.addEventListener("storage", (e) => {
  if (platformNamesOverride !== null) return;
  if (e.key === PLATFORM_NAMES_STORAGE_KEY) {
    updatePlatformNamesPreference(e.newValue !== "false");
  }
});

if (platformNamesChannel) {
  platformNamesChannel.addEventListener("message", (event) => {
    if (platformNamesOverride !== null) return;
    if (event.data?.type === "showPlatformNames") {
      updatePlatformNamesPreference(event.data.value);
    }
  });
}

if (refreshButtonEl) {
  refreshButtonEl.addEventListener("click", () => {
    fetchBootstrap().catch((error) => {
      console.error(error);
    });
  });
}

// Emote picker
const emoteToggle = document.getElementById("emote-toggle");
const emotePicker = document.getElementById("emote-picker");
const emoteGrid = document.getElementById("emote-grid");
const emoteSearch = document.getElementById("emote-search");
const replyInput = document.getElementById("reply-message");
let cachedEmotes = null;

if (emoteToggle && emotePicker) {
  emoteToggle.addEventListener("click", async () => {
    const isOpen = !emotePicker.classList.contains("hidden");
    if (isOpen) {
      emotePicker.classList.add("hidden");
      return;
    }
    emotePicker.classList.remove("hidden");
    if (!cachedEmotes) {
      emoteGrid.innerHTML = '<div class="emote-loading">Loading emotes...</div>';
      try {
        const resp = await fetch("/api/emotes");
        const data = await resp.json();
        cachedEmotes = data.emotes || [];
      } catch (_) {
        cachedEmotes = [];
      }
    }
    renderEmotes("");
    emoteSearch.value = "";
    emoteSearch.focus();
  });

  emoteSearch.addEventListener("input", () => {
    renderEmotes(emoteSearch.value.toLowerCase());
  });

  document.addEventListener("click", (e) => {
    if (!emotePicker.contains(e.target) && e.target !== emoteToggle) {
      emotePicker.classList.add("hidden");
    }
  });
}

function renderEmotes(filter) {
  if (!cachedEmotes) return;
  const filtered = filter
    ? cachedEmotes.filter((e) => e.name.toLowerCase().includes(filter))
    : cachedEmotes;
  emoteGrid.innerHTML = filtered.slice(0, 200).map((e) =>
    `<img class="emote-pick" src="${e.url}" alt="${escapeHtml(e.name)}" title="${escapeHtml(e.name)}" data-name="${escapeHtml(e.name)}">`
  ).join("") || '<div class="emote-loading">No emotes found</div>';

  emoteGrid.querySelectorAll(".emote-pick").forEach((img) => {
    img.addEventListener("click", () => {
      const name = img.dataset.name;
      const input = replyInput;
      const pos = input.selectionStart || input.value.length;
      const before = input.value.slice(0, pos);
      const after = input.value.slice(pos);
      const space = before.length && !before.endsWith(" ") ? " " : "";
      input.value = before + space + name + " " + after;
      input.focus();
    });
  });
}

replyFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(replyFormEl);
  const message = String(formData.get("message") || "").trim();
  if (!message) return;

  const submitBtn = replyFormEl.querySelector('button[type="submit"]');
  if (submitBtn) submitBtn.disabled = true;
  try {
    const response = await fetch("/api/reply/twitch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (response.status === 401) {
      window.location.href = "/login";
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Unknown Twitch send error");
    }
    replyFormEl.reset();
  } catch (error) {
    console.error("Reply failed:", error);
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
});

// Twitch mod actions panel (broadcaster-only feature; no visual affordance on names)
const MOD_CONFIRM_MS = 4000;
const MOD_TIMEOUT_OPTIONS = [
  { label: "5 min", duration: 300 },
  { label: "10 min", duration: 600 },
  { label: "30 min", duration: 1800 },
];
const modPanelState = {
  el: null,
  userId: null,
  anchorRect: null,
  confirmTimer: null,
  busy: false,
};

function ensureModPanel() {
  if (modPanelState.el) return modPanelState.el;
  const panel = document.createElement("div");
  panel.className = "mod-panel hidden";
  panel.addEventListener("click", handleModPanelClick);
  document.body.appendChild(panel);
  modPanelState.el = panel;
  return panel;
}

function isModPanelOpen() {
  return Boolean(modPanelState.el) && !modPanelState.el.classList.contains("hidden");
}

function openModPanel(nameEl) {
  const panel = ensureModPanel();
  resetModConfirm();
  modPanelState.userId = nameEl.dataset.modUserId;
  modPanelState.anchorRect = nameEl.getBoundingClientRect();
  modPanelState.busy = false;
  const durationButtons = MOD_TIMEOUT_OPTIONS.map((option) =>
    `<button type="button" class="mod-panel-button" data-action="timeout" data-duration="${option.duration}" data-label="${option.label}">${option.label}</button>`
  ).join("");
  panel.innerHTML = `
    <div class="mod-panel-user">${escapeHtml(nameEl.dataset.modUserName || "")}</div>
    <div class="mod-panel-actions">
      <button type="button" class="mod-panel-button mod-ban" data-action="ban" data-label="Ban">Ban</button>
      <button type="button" class="mod-panel-button" data-action="timeout-toggle" data-label="Timeout">Timeout</button>
    </div>
    <div class="mod-panel-durations hidden">${durationButtons}</div>
    <div class="mod-panel-status"></div>
  `;
  panel.classList.remove("hidden");
  positionModPanel();
}

function positionModPanel() {
  const panel = modPanelState.el;
  const rect = modPanelState.anchorRect;
  if (!panel || !rect) return;
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;
  let left = Math.min(Math.max(rect.left, 8), window.innerWidth - width - 8);
  let top = rect.bottom + 6;
  if (top + height > window.innerHeight - 8) {
    top = rect.top - height - 6;
  }
  top = Math.max(top, 8);
  panel.style.left = `${Math.max(left, 8)}px`;
  panel.style.top = `${top}px`;
}

function closeModPanel() {
  if (!isModPanelOpen()) return;
  resetModConfirm();
  modPanelState.el.classList.add("hidden");
  modPanelState.userId = null;
  modPanelState.anchorRect = null;
  modPanelState.busy = false;
}

function resetModConfirm() {
  if (modPanelState.confirmTimer) {
    clearTimeout(modPanelState.confirmTimer);
    modPanelState.confirmTimer = null;
  }
  if (!modPanelState.el) return;
  modPanelState.el.querySelectorAll(".mod-panel-button.confirming").forEach((button) => {
    button.classList.remove("confirming");
    button.textContent = button.dataset.label;
  });
}

function handleModPanelClick(event) {
  const button = event.target.closest("button");
  if (!button || modPanelState.busy) return;

  if (button.dataset.action === "timeout-toggle") {
    resetModConfirm();
    modPanelState.el.querySelector(".mod-panel-durations").classList.toggle("hidden");
    positionModPanel();
    return;
  }

  if (!button.classList.contains("confirming")) {
    resetModConfirm();
    button.classList.add("confirming");
    button.textContent = "Confirm?";
    modPanelState.confirmTimer = window.setTimeout(resetModConfirm, MOD_CONFIRM_MS);
    return;
  }

  resetModConfirm();
  const duration = button.dataset.duration ? Number(button.dataset.duration) : null;
  sendModAction(duration);
}

async function sendModAction(duration) {
  const panel = modPanelState.el;
  const statusEl = panel.querySelector(".mod-panel-status");
  modPanelState.busy = true;
  panel.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  statusEl.classList.remove("error");
  statusEl.textContent = "Sending...";

  const endpoint = duration ? "/api/mod/twitch/timeout" : "/api/mod/twitch/ban";
  const body = { user_id: modPanelState.userId };
  if (duration) body.duration = duration;

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      window.location.href = "/login";
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Moderation request failed");
    }
    if (payload.result?.already_banned) {
      statusEl.textContent = "Already banned";
    } else {
      statusEl.textContent = duration ? `Timed out ${Math.round(duration / 60)} min` : "Banned";
    }
    positionModPanel();
  } catch (error) {
    statusEl.classList.add("error");
    statusEl.textContent = String(error.message || error);
    modPanelState.busy = false;
    panel.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
    positionModPanel();
  }
}

document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const nameEl = target?.closest(".author-name[data-mod-user-id]");
  if (nameEl) {
    if (isModPanelOpen() && modPanelState.userId === nameEl.dataset.modUserId) {
      closeModPanel();
    } else {
      openModPanel(nameEl);
    }
    return;
  }
  if (isModPanelOpen() && !modPanelState.el.contains(target)) {
    closeModPanel();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModPanel();
});

feedEl.addEventListener("scroll", () => {
  if (performance.now() < modScrollCloseSuppressedUntil) return;
  closeModPanel();
});
window.addEventListener("resize", closeModPanel);

fetchBootstrap()
  .then(connectSocket)
  .catch((error) => {
    console.error(error);
    connectSocket();
  });
