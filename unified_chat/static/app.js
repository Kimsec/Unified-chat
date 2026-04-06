const state = {
  messages: [],
  statuses: new Map(),
  filters: {
    twitch: true,
    youtube: true,
    kick: true,
  },
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


function renderMessageText(text, emotes) {
  if (!emotes || !emotes.length) return escapeHtml(text);
  const sorted = [...emotes].sort((a, b) => a.begin - b.begin);
  let result = "";
  let cursor = 0;
  for (const emote of sorted) {
    if (emote.begin > cursor) {
      result += escapeHtml(text.slice(cursor, emote.begin));
    }
    result += `<img class="emote" src="https://static-cdn.jtvnw.net/emoticons/v2/${emote.id}/default/dark/1.0" alt="${escapeHtml(emote.text)}" title="${escapeHtml(emote.text)}">`;
    cursor = emote.end;
  }
  if (cursor < text.length) {
    result += escapeHtml(text.slice(cursor));
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
    const authorStyle = message.author_color ? `style="color:${message.author_color}"` : "";
    const sourceBroadcaster = message.raw_payload?.payload?.event?.source_broadcaster || null;
    const isSystemNotice = message.platform === "twitch" && message.message_kind === "system";
    const sourceAvatar = message.platform === "twitch" && message.avatar_url
      ? `<img class="source-streamer-avatar" src="${escapeHtml(message.avatar_url)}" alt="" title="${escapeHtml(sourceBroadcaster?.name || sourceBroadcaster?.login || "Shared chat source")}" aria-hidden="true">`
      : "";

    if (isSystemNotice) {
      return `
        <article class="message-card system-notice" data-platform="${message.platform}">
          <span class="message-topline"><span class="message-time">${formatTime(message.sent_at)}</span> ${platformMarkup(message.platform)}${sourceAvatar}<span class="message-text system-notice-text">${renderMessageText(message.text, message.emotes)}</span></span>
        </article>
      `;
    }

    return `
      <article class="message-card" data-platform="${message.platform}">
        <span class="message-topline"><span class="message-time">${formatTime(message.sent_at)}</span> ${platformMarkup(message.platform)}${sourceAvatar}<span class="author-name" ${authorStyle}>${escapeHtml(message.author_display_name)}:</span> <span class="message-text">${renderMessageText(message.text, message.emotes)}</span></span>
      </article>
    `;
  }).join("");

  requestAnimationFrame(() => {
    if (wasNearBottom) {
      feedEl.scrollTop = feedEl.scrollHeight;
    }
  });
}

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
  }
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

function connectSocket() {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${window.location.host}/ws/chat`);
  socket.addEventListener("message", (event) => {
    try {
      handleSocketPayload(JSON.parse(event.data));
    } catch (_) {}
  });
  socket.addEventListener("open", () => {
    socket.send("ready");
  });
  socket.addEventListener("close", () => {
    window.setTimeout(connectSocket, 3000);
  });
}

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

fetchBootstrap()
  .then(connectSocket)
  .catch((error) => {
    console.error(error);
    connectSocket();
  });
