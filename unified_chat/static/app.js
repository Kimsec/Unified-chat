const isPopout = document.body.dataset.mode === "popout";
const pageParams = new URLSearchParams(window.location.search);

// Overlay mode (/overlay): transparent OBS variant with auto-fading messages.
const isOverlay = isPopout && (window.location.pathname === "/overlay" || pageParams.has("overlay"));
const OVERLAY_FADE_MS = Math.max(Number(pageParams.get("fade")) || 60, 5) * 1000;
const OVERLAY_FADE_OUT_MS = 2500;
const overlayOptions = {
  size: Math.min(Math.max(Number(pageParams.get("size")) || 0, 0), 64),
  alignRight: pageParams.get("align") === "right",
  max: Math.min(Math.max(Number(pageParams.get("max")) || 0, 0), 200),
  icons: pageParams.get("icons") !== "0",
};
if (isOverlay) {
  const root = document.documentElement;
  root.classList.add("overlay-mode");
  if (overlayOptions.size) root.style.setProperty("--overlay-font", `${overlayOptions.size}px`);
  if (overlayOptions.alignRight) root.classList.add("overlay-align-right");
  if (!overlayOptions.icons) root.classList.add("overlay-no-icons");
}

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
const MAX_VISIBLE_MESSAGES = isOverlay && overlayOptions.max ? overlayOptions.max : 200;

const feedEl = document.getElementById("feed");
const statusGridEl = document.getElementById("status-grid");
const replyFormEl = document.getElementById("reply-form");
const replyStatusEl = document.getElementById("reply-status");
const refreshButtonEl = document.getElementById("refresh-status");

const PLATFORM_SVGS = {
  twitch: `<svg viewBox="0 0 256 268" aria-hidden="true"><path fill="#9146ff" d="M17.46 0L0 46.56v185.21h63.14V268h46.87l36.49-36.23h54.91L256 177.68V0H17.46zm23.07 23.07H232.9v143.14l-41.47 41.47h-69.15L85.79 244.2v-36.52H40.53V23.07zm69.15 104.55h23.07V69.26h-23.07v58.36zm63.14 0h23.07V69.26h-23.07v58.36z"/></svg>`,
  youtube: `<svg viewBox="0 0 576 512" aria-hidden="true"><path fill="#ff0000" d="M549.66 124.63a68.28 68.28 0 0 0-48.05-48.28C458.78 64 288 64 288 64S117.22 64 74.39 76.35a68.28 68.28 0 0 0-48.05 48.28C14.48 167.83 14.48 256 14.48 256s0 88.17 11.86 131.37a68.28 68.28 0 0 0 48.05 48.28C117.22 448 288 448 288 448s170.78 0 213.61-12.35a68.28 68.28 0 0 0 48.05-48.28C561.52 344.17 561.52 256 561.52 256s0-88.17-11.86-131.37zM232.15 337.28V174.72L374.86 256l-142.71 81.28z"/></svg>`,
  kick: `<img src="/static/kick-logo.ico" aria-hidden="true">`,
};
// Server-stored display settings; URL params (platform/badges/emotes/mentions/clock/size) override per page.
const SETTING_URL_PARAMS = {
  showPlatform: "platform",
  showBadges: "badges",
  showThirdPartyEmotes: "emotes",
  highlightMentions: "mentions",
  use24hClock: "clock",
};
const settings = {
  showPlatform: true,
  showBadges: true,
  showThirdPartyEmotes: true,
  highlightMentions: true,
  use24hClock: true,
  chatFontPx: 16,
  alertUrls: [],
};
const settingOverrides = {};
for (const [key, param] of Object.entries(SETTING_URL_PARAMS)) {
  const value = pageParams.get(param);
  if (value !== null) settingOverrides[key] = value !== "0";
}
const sizeParam = Number(pageParams.get("size"));
if (sizeParam) settingOverrides.chatFontPx = Math.min(Math.max(sizeParam, 12), 24);

function settingOn(key) {
  return settingOverrides[key] ?? settings[key];
}

function applySettings(next) {
  Object.assign(settings, next || {});
  for (const [key, toggle] of Object.entries(settingToggles)) {
    toggle.classList.toggle("active", settingOn(key));
  }
  applyChatFont();
  renderAlertFrames();
  syncAlertsEditButton();
}

function applyChatFont() {
  const px = settingOn("chatFontPx");
  document.documentElement.style.setProperty("--chat-font", `${px}px`);
  if (fontSlider) {
    fontSlider.value = px;
    fontSizeValue.textContent = `${px}px`;
  }
}

async function updateSetting(key, value) {
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: value }),
    });
    if (response.status === 401) {
      window.location.href = "/login";
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Unknown settings error");
    }
    applySettings(payload.settings);
    renderMessages();
  } catch (error) {
    console.error("Setting update failed:", error);
  }
}

function platformMarkup(platform) {
  return `<span class="platform-pill ${platform}">${PLATFORM_SVGS[platform]}</span>`;
}

const TWITCH_BADGE_IDS = {
  broadcaster: "5527c58c-fb7d-422d-b71b-f309dcb85cc1",
  moderator: "3267646d-33f0-4b17-b3df-f923a41db1d0",
  vip: "b817aba4-fad8-49e2-b88a-7cc744dfa6ec",
  partner: "d12a2e27-16f6-41d0-ab77-b780518f00a3",
  subscriber: "5d9f2208-5dd8-11e7-8513-2ff4adfae661",
  founder: "511b78a9-ab37-472f-9569-457753bbe7d3",
  premium: "bbbe0db0-a598-423e-86d0-f9fb98ca1933",
  turbo: "bd444ec6-8f34-4bf9-91f4-af1e3428d80f",
  staff: "d97c37bd-a6f5-4c38-8f57-4e4bef88af34",
  "sub-gifter": "f1d8486f-eb2e-4553-b44f-4d614617afc1",
};

function badgesMarkup(message) {
  if (!settingOn("showBadges") || message.platform !== "twitch" || !message.badges?.length) return "";
  return message.badges.map((badge) => {
    const name = String(badge.type || "").toLowerCase();
    const id = TWITCH_BADGE_IDS[name];
    if (!id) return "";
    return `<img class="badge" src="https://static-cdn.jtvnw.net/badges/v1/${id}/1" alt="${escapeHtml(name)}" title="${escapeHtml(name)}">`;
  }).join("");
}

// Third-party Twitch emotes (7TV/BTTV/FFZ), name → url, sent by the server.
let thirdPartyEmotes = new Map();

// Alert sounds: hidden alert-overlay iframes, active in the popout and expanded chat.
const alertFramesEl = document.getElementById("alert-frames");
const alertsUnlockEl = document.getElementById("alerts-unlock");
const alertsEditBtn = document.getElementById("alerts-edit");
let alertAudioUnlocked = false;

function isValidAlertUrl(value) {
  try {
    return new URL(value).protocol === "https:";
  } catch (_) {
    return false;
  }
}

function savedAlertUrls() {
  return (Array.isArray(settings.alertUrls) ? settings.alertUrls : []).filter(isValidAlertUrl);
}

function activeAlertUrls() {
  if (isOverlay || pageParams.get("alerts") === "0") return [];
  if (!isPopout && !isExpanded) return [];
  return savedAlertUrls();
}

function renderAlertFrames() {
  if (!alertFramesEl) return;
  const urls = activeAlertUrls();
  for (const frame of [...alertFramesEl.children]) {
    if (!urls.includes(frame.src)) frame.remove();
  }
  const existing = new Set([...alertFramesEl.children].map((frame) => frame.src));
  for (const url of urls) {
    if (existing.has(url)) continue;
    const frame = document.createElement("iframe");
    frame.src = url;
    frame.allow = "autoplay";
    frame.tabIndex = -1;
    alertFramesEl.appendChild(frame);
  }
  updateAlertUnlock();
}

function unlockAlertAudio() {
  alertAudioUnlocked = true;
  updateAlertUnlock();
  for (const frame of alertFramesEl?.children || []) {
    frame.contentWindow?.postMessage({ type: "unlock-audio" }, "*");
  }
}

function updateAlertUnlock() {
  alertsUnlockEl?.classList.toggle("hidden", alertAudioUnlocked || !activeAlertUrls().length);
}

function syncAlertsEditButton() {
  if (!alertsEditBtn) return;
  const count = savedAlertUrls().length;
  alertsEditBtn.textContent = count ? `✎ ${count}` : "+ Add";
  alertsEditBtn.title = count ? "Edit alert sound overlays" : "Add alert sound overlays (StreamElements etc.)";
}

if (alertsUnlockEl) {
  alertsUnlockEl.addEventListener("click", unlockAlertAudio);
  const onFirstGesture = () => {
    if (!activeAlertUrls().length) return;
    unlockAlertAudio();
    document.removeEventListener("pointerdown", onFirstGesture);
  };
  document.addEventListener("pointerdown", onFirstGesture);
}

function emoteImg(url, name) {
  return `<img class="emote" src="${escapeHtml(url)}" alt="${escapeHtml(name)}" title="${escapeHtml(name)}">`;
}

// Global cheermotes from Twitch's open CDN, only applied to messages that carried bits.
const CHEERMOTE_PREFIXES = new Set([
  "cheer", "cheerwhal", "corgo", "uni", "showlove", "party", "seemsgood", "pride",
  "kappa", "frankerz", "heyguys", "dansgame", "trihard", "kreygasm", "4head",
  "swiftrage", "notlikethis", "failfish", "vohiyo", "pjsalt", "mrdestructoid",
  "bday", "ripcheer", "shamrock",
]);
const CHEER_TIERS = [
  [10000, "#f43021"],
  [5000, "#0099fe"],
  [1000, "#1db2a5"],
  [100, "#9c3ee8"],
  [1, "#979797"],
];

function cheermoteMarkup(word) {
  const match = /^(.+?)(\d+)$/.exec(word);
  if (!match) return null;
  const prefix = match[1].toLowerCase();
  if (!CHEERMOTE_PREFIXES.has(prefix)) return null;
  const amount = Number(match[2]);
  const [tier, color] = CHEER_TIERS.find(([min]) => amount >= min) || CHEER_TIERS[CHEER_TIERS.length - 1];
  const src = `https://d3aqoihi2n8ty8.cloudfront.net/actions/${prefix}/dark/animated/${tier}/1.gif`;
  return `<img class="emote" src="${src}" alt="${escapeHtml(word)}" title="${escapeHtml(word)}"><span class="cheer-amount" style="color:${color}">${amount}</span>`;
}

function renderPlainText(text, platform, bits) {
  if (platform !== "twitch") return linkifyText(text);
  const useEmotes = settingOn("showThirdPartyEmotes") && thirdPartyEmotes.size;
  if (!useEmotes && !bits) return linkifyText(text);
  return text.split(" ").map((word) => {
    if (bits) {
      const cheer = cheermoteMarkup(word);
      if (cheer) return cheer;
    }
    const url = useEmotes ? thirdPartyEmotes.get(word) : undefined;
    return url ? emoteImg(url, word) : linkifyText(word);
  }).join(" ");
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

function renderMessageText(text, emotes, platform, bits = 0) {
  if (!emotes || !emotes.length) return renderPlainText(text, platform, bits);
  const emoteUrl = EMOTE_IMAGE_URLS[platform] || EMOTE_IMAGE_URLS.twitch;
  // Array.from splits by code points, matching the server's Python indexing.
  const chars = Array.from(text);
  const sorted = [...emotes].sort((a, b) => a.begin - b.begin);
  let result = "";
  let cursor = 0;
  for (const emote of sorted) {
    if (emote.begin > cursor) {
      result += renderPlainText(chars.slice(cursor, emote.begin).join(""), platform, bits);
    }
    result += emoteImg(emoteUrl(encodeURIComponent(emote.id)), emote.text);
    cursor = emote.end;
  }
  if (cursor < chars.length) {
    result += renderPlainText(chars.slice(cursor).join(""), platform, bits);
  }
  return result;
}

function formatTime(isoString) {
  try {
    return new Date(isoString).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: !settingOn("use24hClock") });
  } catch (_) {
    return "";
  }
}

// Broadcaster names for mention highlighting, collected from raw Twitch/Kick payloads.
const mentionNames = new Set();
let mentionRegex = null;

function noteMentionNames(message) {
  const event = message.raw_payload?.payload?.event;
  const broadcaster = message.raw_payload?.broadcaster;
  for (const name of [
    event?.broadcaster_user_login,
    event?.broadcaster_user_name,
    broadcaster?.username,
    broadcaster?.channel_slug,
  ]) {
    if (!name) continue;
    const normalized = String(name).toLowerCase();
    if (!mentionNames.has(normalized)) {
      mentionNames.add(normalized);
      mentionRegex = null;
    }
  }
}

function isMention(message) {
  if (!settingOn("highlightMentions") || message.message_kind === "system") return false;
  if (!mentionRegex && mentionNames.size) {
    const escaped = [...mentionNames].map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    mentionRegex = new RegExp(`@(?:${escaped.join("|")})\\b`, "i");
  }
  return Boolean(mentionRegex?.test(message.text));
}

function normalizeMessages(messages) {
  const map = new Map();
  for (const message of messages) {
    map.set(message.id, message);
    noteMentionNames(message);
  }
  state.messages = Array.from(map.values())
    .sort((a, b) => new Date(a.sent_at) - new Date(b.sent_at))
    .slice(-MAX_VISIBLE_MESSAGES);
}

// Negative delay resumes the fade mid-life after a DOM rebuild.
function overlayFadeStyle(message) {
  if (!isOverlay) return "";
  const age = Math.max(Date.now() - new Date(message.sent_at).getTime(), 0);
  return ` style="animation-delay: ${OVERLAY_FADE_MS - OVERLAY_FADE_OUT_MS - age}ms"`;
}

function renderMessages() {
  const visibleMessages = state.messages.filter((message) => state.filters[message.platform] !== false);

  if (!visibleMessages.length) {
    feedEl.innerHTML = `<div class="empty-state">No messages yet.</div>`;
    return;
  }

  const wasNearBottom = isNearBottom();

  feedEl.innerHTML = visibleMessages.map((message) => {
    let messageClass = message.deleted_at ? "message-card deleted" : "message-card";
    if (isMention(message)) messageClass += " mention";
    const readableColor = ensureReadableColor(message.author_color);
    const authorStyle = readableColor ? `style="color:${readableColor}"` : "";
    const sourceBroadcaster = message.raw_payload?.payload?.event?.source_broadcaster || null;
    const isSystemNotice = message.message_kind === "system";
    const sourceAvatar = message.platform === "twitch" && message.avatar_url
      ? `<img class="source-streamer-avatar" src="${escapeHtml(message.avatar_url)}" alt="" title="${escapeHtml(sourceBroadcaster?.name || sourceBroadcaster?.login || "Shared chat source")}" aria-hidden="true">`
      : "";

    if (isSystemNotice) {
      return `
        <article class="${messageClass} system-notice" data-platform="${message.platform}"${overlayFadeStyle(message)}>
          <span class="message-topline"><span class="message-time">${formatTime(message.sent_at)}</span> ${settingOn("showPlatform") ? platformMarkup(message.platform) : ""}${sourceAvatar}<span class="message-text system-notice-text">${renderMessageText(message.text, message.emotes, message.platform, message.bits)}</span></span>
        </article>
      `;
    }

    const canModerate = (message.platform === "twitch" || message.platform === "kick")
      && message.author_id
      && message.author_id !== message.channel_id;
    const modAttrs = canModerate
      ? ` data-mod-user-id="${escapeHtml(message.author_id)}" data-mod-user-name="${escapeHtml(message.author_display_name)}" data-mod-platform="${message.platform}" data-mod-message-id="${escapeHtml(message.platform_message_id || "")}"`
      : "";

    return `
      <article class="${messageClass}" data-platform="${message.platform}"${overlayFadeStyle(message)}>
        <span class="message-topline"><span class="message-time">${formatTime(message.sent_at)}</span> ${settingOn("showPlatform") ? platformMarkup(message.platform) : ""}${sourceAvatar}${badgesMarkup(message)}<span class="author-name" ${authorStyle}${modAttrs}>${escapeHtml(message.author_display_name)}:</span> <span class="message-text">${renderMessageText(message.text, message.emotes, message.platform, message.bits)}</span></span>
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
  thirdPartyEmotes = new Map(Object.entries(payload.third_party_emotes || {}));
  applySettings(payload.settings);
  renderStatuses();
  renderMessages();
  handleHypeTrain(payload.hype_train ?? null);
  handlePoll(payload.poll ?? null);
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
  if (payload.type === "third_party_emotes") {
    thirdPartyEmotes = new Map(Object.entries(payload.emotes || {}));
    renderMessages();
    return;
  }
  if (payload.type === "settings" && payload.settings) {
    applySettings(payload.settings);
    renderMessages();
    return;
  }
  if (payload.type === "hype_train") {
    handleHypeTrain(payload);
    return;
  }
  if (payload.type === "poll") {
    handlePoll(payload);
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
  if (isOverlay) return;
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

let pollEndTimer = null;

function resetPollBar() {
  if (pollEndTimer) {
    clearTimeout(pollEndTimer);
    pollEndTimer = null;
  }
  const bar = document.getElementById("poll-bar");
  if (!bar) return;
  bar.classList.add("hidden");
  bar.setAttribute("aria-hidden", "true");
}

function handlePoll(data) {
  if (isOverlay) return;
  if (!data) {
    resetPollBar();
    return;
  }
  if (pollEndTimer) {
    clearTimeout(pollEndTimer);
    pollEndTimer = null;
  }
  renderPoll(data);
  if (data.phase === "end") {
    pollEndTimer = window.setTimeout(resetPollBar, data.hide_after_ms ?? 18000);
  }
}

function pollChoiceMarkup(choice, pct, winner) {
  return `
    <div class="poll-choice${winner ? " winner" : ""}">
      <div class="poll-choice-info"><span class="poll-choice-title">${escapeHtml(choice.title)}</span><span class="poll-choice-votes">${choice.votes} · ${pct}%</span></div>
      <div class="poll-choice-track"><div class="poll-choice-fill" style="width:${pct}%"></div></div>
    </div>
  `;
}

function renderPoll(data) {
  const bar = document.getElementById("poll-bar");
  if (!bar) return;
  bar.classList.remove("hidden");
  bar.setAttribute("aria-hidden", "false");
  const choices = (data.choices || []).map((choice) => ({ title: choice.title || "", votes: choice.votes || 0 }));
  const total = choices.reduce((sum, choice) => sum + choice.votes, 0);
  const maxVotes = Math.max(0, ...choices.map((choice) => choice.votes));
  const ended = data.phase === "end";
  document.getElementById("poll-title").textContent = data.title || "Poll";
  document.getElementById("poll-status").textContent = ended
    ? `Final · ${total} vote${total === 1 ? "" : "s"}`
    : `${total} vote${total === 1 ? "" : "s"}`;

  const rowsEl = document.getElementById("poll-choices");
  const existing = rowsEl.querySelectorAll(".poll-choice");
  if (existing.length === choices.length && choices.length) {
    // Update in place so the fill bars animate between votes.
    choices.forEach((choice, index) => {
      const pct = total ? Math.round((choice.votes / total) * 100) : 0;
      const row = existing[index];
      row.classList.toggle("winner", ended && choice.votes === maxVotes && maxVotes > 0);
      row.querySelector(".poll-choice-title").textContent = choice.title;
      row.querySelector(".poll-choice-votes").textContent = `${choice.votes} · ${pct}%`;
      row.querySelector(".poll-choice-fill").style.width = `${pct}%`;
    });
    return;
  }
  rowsEl.innerHTML = choices.map((choice) => {
    const pct = total ? Math.round((choice.votes / total) * 100) : 0;
    return pollChoiceMarkup(choice, pct, ended && choice.votes === maxVotes && maxVotes > 0);
  }).join("");
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

const infoOverlayEl = document.getElementById("info-overlay");
const openInfoBtn = document.getElementById("open-info");
if (infoOverlayEl && openInfoBtn) {
  const closeInfo = () => infoOverlayEl.classList.add("hidden");
  openInfoBtn.addEventListener("click", () => infoOverlayEl.classList.remove("hidden"));
  document.getElementById("close-info").addEventListener("click", closeInfo);
  infoOverlayEl.addEventListener("click", (event) => {
    if (event.target === infoOverlayEl) closeInfo();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !infoOverlayEl.classList.contains("hidden")) closeInfo();
  });
}

// Expand (?expand=1): the main page expands into the popout layout in place.
const expandToggleBtn = document.getElementById("expand-toggle");
const feedPanelEl = document.querySelector(".feed-panel");
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let isExpanded = Boolean(expandToggleBtn) && (pageParams.get("expand") ?? "0") !== "0";
document.documentElement.classList.toggle("expand-mode", isExpanded);
let expandAnimCleanup = null;

function setExpanded(on, { animate = true } = {}) {
  if (!expandToggleBtn || on === isExpanded) return;
  closeModPanel();
  const firstRect = feedPanelEl.getBoundingClientRect();
  const wasNearBottom = isNearBottom();
  isExpanded = on;
  document.documentElement.classList.toggle("expand-mode", on);

  const params = new URLSearchParams(window.location.search);
  if (on) {
    params.set("expand", "1");
  } else {
    params.delete("expand");
  }
  const query = params.toString();
  window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);

  renderAlertFrames();
  if (animate && !prefersReducedMotion) playExpandTransition(firstRect);
  if (wasNearBottom) {
    requestAnimationFrame(() => {
      feedEl.scrollTop = feedEl.scrollHeight;
    });
  }
}

// FLIP: slide the feed panel from its old rect to where the class flip put it.
function playExpandTransition(firstRect) {
  expandAnimCleanup?.();
  const lastRect = feedPanelEl.getBoundingClientRect();
  if (!lastRect.width || !lastRect.height) return;
  feedPanelEl.classList.add("expand-anim");
  feedPanelEl.style.transition = "none";
  feedPanelEl.style.transformOrigin = "top left";
  feedPanelEl.style.transform = `translate(${firstRect.left - lastRect.left}px, ${firstRect.top - lastRect.top}px) `
    + `scale(${firstRect.width / lastRect.width}, ${firstRect.height / lastRect.height})`;
  feedPanelEl.getBoundingClientRect(); // flush, so the transition has a start frame
  feedPanelEl.style.transition = "transform 320ms cubic-bezier(0.2, 0.8, 0.2, 1)";
  feedPanelEl.style.transform = "";
  const finish = () => {
    feedPanelEl.classList.remove("expand-anim");
    feedPanelEl.style.transition = "";
    feedPanelEl.style.transformOrigin = "";
    feedPanelEl.style.transform = "";
    feedPanelEl.removeEventListener("transitionend", finish);
    clearTimeout(timer);
    expandAnimCleanup = null;
  };
  const timer = setTimeout(finish, 400);
  feedPanelEl.addEventListener("transitionend", finish);
  expandAnimCleanup = finish;
}

if (expandToggleBtn) {
  expandToggleBtn.addEventListener("click", () => setExpanded(true));
  document.getElementById("expand-exit").addEventListener("click", () => setExpanded(false));
  // Registered before the mod panel's Escape handler, so an open panel only closes.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isExpanded) return;
    if (isModPanelOpen()) return;
    if (infoOverlayEl && !infoOverlayEl.classList.contains("hidden")) return;
    setExpanded(false);
  });
}

const settingToggles = {};
for (const [key, id] of Object.entries({
  showPlatform: "toggle-platform",
  showBadges: "toggle-badges",
  showThirdPartyEmotes: "toggle-emotes",
  highlightMentions: "toggle-mentions",
  use24hClock: "toggle-clock",
})) {
  const toggle = document.getElementById(id);
  if (!toggle) continue;
  settingToggles[key] = toggle;
  toggle.classList.toggle("active", settingOn(key));
  toggle.addEventListener("click", () => updateSetting(key, !settingOn(key)));
}

// Live preview while dragging; saved to the server on release.
const fontSlider = document.getElementById("font-size");
const fontSizeValue = document.getElementById("font-size-value");
if (fontSlider) {
  fontSlider.addEventListener("input", () => {
    document.documentElement.style.setProperty("--chat-font", `${fontSlider.value}px`);
    fontSizeValue.textContent = `${fontSlider.value}px`;
  });
  fontSlider.addEventListener("change", () => {
    updateSetting("chatFontPx", Number(fontSlider.value));
  });
}
applyChatFont();

if (alertsEditBtn) {
  const editorEl = document.getElementById("alerts-editor");
  const rowsEl = document.getElementById("alerts-rows");
  const applyBtn = document.getElementById("alerts-apply");

  const addRow = (value = "") => {
    const row = document.createElement("div");
    row.className = "alerts-row";
    row.innerHTML = `
      <input class="input-field alerts-input" type="url" placeholder="https://streamelements.com/overlay/…" spellcheck="false">
      <button class="alerts-remove" type="button" title="Remove">&#x2715;</button>`;
    row.querySelector("input").value = value;
    rowsEl.appendChild(row);
  };

  alertsEditBtn.addEventListener("click", () => {
    if (editorEl.classList.contains("hidden")) {
      rowsEl.innerHTML = "";
      const urls = savedAlertUrls();
      (urls.length ? urls : [""]).forEach(addRow);
      editorEl.classList.remove("hidden");
      rowsEl.querySelector("input")?.focus();
    } else {
      editorEl.classList.add("hidden"); // discard edits; reopening re-reads saved state
    }
  });

  rowsEl.addEventListener("click", (event) => {
    const remove = event.target.closest(".alerts-remove");
    if (!remove) return;
    remove.closest(".alerts-row").remove();
    // Removing the last row deletes the saved list and closes the editor.
    if (!rowsEl.children.length) {
      updateSetting("alertUrls", []);
      editorEl.classList.add("hidden");
    }
  });

  rowsEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      applyBtn.click();
    }
  });

  document.getElementById("alerts-add-row").addEventListener("click", () => {
    addRow();
    rowsEl.lastElementChild.querySelector("input").focus();
  });

  applyBtn.addEventListener("click", () => {
    let valid = true;
    const urls = [];
    for (const input of rowsEl.querySelectorAll(".alerts-input")) {
      const value = input.value.trim();
      input.classList.remove("invalid");
      if (!value) continue;
      if (isValidAlertUrl(value)) {
        urls.push(value);
      } else {
        input.classList.add("invalid");
        valid = false;
      }
    }
    if (!valid) return;
    updateSetting("alertUrls", urls);
    editorEl.classList.add("hidden");
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

// Reply target (Twitch / Kick / both), picked from the icon inside the input field.
const PLATFORM_LABELS = { twitch: "Twitch", kick: "Kick" };
const replyTargetToggle = document.getElementById("reply-target-toggle");
const replyTargetMenu = document.getElementById("reply-target-menu");
const replyContextEl = document.getElementById("reply-context");
const replyContextTextEl = document.getElementById("reply-context-text");
const TARGET_ICONS = {
  twitch: PLATFORM_SVGS.twitch,
  kick: PLATFORM_SVGS.kick,
  all: `<span class="reply-target-all">ALL</span>`,
};
let replyTarget = "twitch";
try {
  replyTarget = window.localStorage.getItem("replyTarget") || "twitch";
} catch (_) {}
if (!["twitch", "kick", "all"].includes(replyTarget)) replyTarget = "twitch";
let replyContext = null;

function setReplyTarget(target) {
  replyTarget = target;
  try {
    window.localStorage.setItem("replyTarget", target);
  } catch (_) {}
  replyTargetToggle.innerHTML = TARGET_ICONS[target];
  replyTargetToggle.title = target === "all" ? "Sending to Twitch + Kick" : `Sending to ${PLATFORM_LABELS[target]}`;
  replyTargetMenu.querySelectorAll(".reply-target-option").forEach((button) => {
    button.classList.toggle("active", button.dataset.target === target);
  });
  replyInput.placeholder = target === "all" ? "Reply to Twitch + Kick..." : `Reply to ${PLATFORM_LABELS[target]}...`;
  if (replyContext && target !== "all" && target !== replyContext.platform) clearReplyContext();
}

function setReplyContext(platform, messageId, author) {
  replyContext = { platform, messageId, author };
  replyContextTextEl.textContent = `↩ Replying to ${author} on ${PLATFORM_LABELS[platform]}`;
  replyContextEl.classList.remove("hidden");
  if (replyTarget !== "all" && replyTarget !== platform) setReplyTarget(platform);
  replyInput.focus();
}

function clearReplyContext() {
  replyContext = null;
  replyContextEl.classList.add("hidden");
}

if (replyTargetToggle) {
  replyTargetToggle.addEventListener("click", () => {
    replyTargetMenu.classList.toggle("hidden");
  });
  replyTargetMenu.addEventListener("click", (event) => {
    const button = event.target.closest(".reply-target-option");
    if (!button) return;
    setReplyTarget(button.dataset.target);
    replyTargetMenu.classList.add("hidden");
    replyInput.focus();
  });
  document.addEventListener("click", (event) => {
    if (!replyTargetMenu.contains(event.target) && !replyTargetToggle.contains(event.target)) {
      replyTargetMenu.classList.add("hidden");
    }
  });
  setReplyTarget(replyTarget);
  document.getElementById("reply-context-clear").addEventListener("click", clearReplyContext);
}

replyInput.addEventListener("input", () => {
  replyStatusEl.textContent = "";
});

replyFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = replyInput.value.trim();
  if (!message) return;

  const submitBtn = replyFormEl.querySelector('button[type="submit"]');
  if (submitBtn) submitBtn.disabled = true;
  replyStatusEl.textContent = "";
  const targets = replyTarget === "all" ? ["twitch", "kick"] : [replyTarget];
  const errors = [];
  await Promise.all(targets.map(async (platform) => {
    try {
      const body = { message };
      if (replyContext?.platform === platform) body.reply_to_message_id = replyContext.messageId;
      const response = await fetch(`/api/reply/${platform}`, {
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
        throw new Error(payload.detail || "Unknown send error");
      }
    } catch (error) {
      errors.push(`${PLATFORM_LABELS[platform]}: ${error.message || error}`);
    }
  }));
  if (errors.length) {
    replyStatusEl.textContent = errors.join(" · ");
  } else {
    replyFormEl.reset();
    clearReplyContext();
  }
  if (submitBtn) submitBtn.disabled = false;
});

// Twitch/Kick mod actions panel (broadcaster-only feature; no visual affordance on names)
const MOD_CONFIRM_MS = 4000;
const MOD_TIMEOUT_OPTIONS = [
  { label: "5 min", duration: 300 },
  { label: "10 min", duration: 600 },
  { label: "30 min", duration: 1800 },
];
const modPanelState = {
  el: null,
  userId: null,
  userName: null,
  platform: null,
  messageId: null,
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
  modPanelState.userName = nameEl.dataset.modUserName || "";
  modPanelState.platform = nameEl.dataset.modPlatform || "twitch";
  modPanelState.messageId = nameEl.dataset.modMessageId || null;
  modPanelState.anchorRect = nameEl.getBoundingClientRect();
  modPanelState.busy = false;
  const durationButtons = MOD_TIMEOUT_OPTIONS.map((option) =>
    `<button type="button" class="mod-panel-button" data-action="timeout" data-duration="${option.duration}" data-label="${option.label}">${option.label}</button>`
  ).join("");
  const replyButton = modPanelState.messageId
    ? `<button type="button" class="mod-panel-button" data-action="reply" data-label="Reply">Reply</button>`
    : "";
  const deleteButton = modPanelState.messageId
    ? `<button type="button" class="mod-panel-button" data-action="delete" data-label="Delete msg">Delete msg</button>`
    : "";
  panel.innerHTML = `
    <div class="mod-panel-user">${escapeHtml(nameEl.dataset.modUserName || "")}</div>
    <div class="mod-panel-actions">
      ${replyButton}
      ${deleteButton}
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
  modPanelState.userName = null;
  modPanelState.platform = null;
  modPanelState.messageId = null;
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

  if (button.dataset.action === "reply") {
    setReplyContext(modPanelState.platform, modPanelState.messageId, modPanelState.userName);
    closeModPanel();
    return;
  }

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
  sendModAction(button.dataset.action, duration);
}

async function sendModAction(action, duration) {
  const panel = modPanelState.el;
  const statusEl = panel.querySelector(".mod-panel-status");
  modPanelState.busy = true;
  panel.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  statusEl.classList.remove("error");
  statusEl.textContent = "Sending...";

  const platform = modPanelState.platform || "twitch";
  const isDelete = action === "delete";
  const endpoint = isDelete
    ? `/api/mod/${platform}/delete-message`
    : duration ? `/api/mod/${platform}/timeout` : `/api/mod/${platform}/ban`;
  const body = isDelete
    ? { message_id: modPanelState.messageId }
    : { user_id: modPanelState.userId };
  if (!isDelete && duration) body.duration = duration;

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
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(payload?.detail || `Moderation request failed (${response.status})`);
    }
    if (payload?.result?.already_banned) {
      statusEl.textContent = "Already banned";
    } else if (isDelete) {
      statusEl.textContent = "Deleted";
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
    if (
      isModPanelOpen()
      && modPanelState.userId === nameEl.dataset.modUserId
      && modPanelState.messageId === (nameEl.dataset.modMessageId || null)
    ) {
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

if (isOverlay) {
  // Prune messages the fade animation has already hidden.
  setInterval(() => {
    const cutoff = Date.now() - OVERLAY_FADE_MS;
    const kept = state.messages.filter((message) => new Date(message.sent_at).getTime() >= cutoff);
    if (kept.length !== state.messages.length) {
      state.messages = kept;
      renderMessages();
    }
  }, 1000);
}

fetchBootstrap()
  .then(connectSocket)
  .catch((error) => {
    console.error(error);
    connectSocket();
  });
