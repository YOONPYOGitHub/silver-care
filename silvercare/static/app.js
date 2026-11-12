"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  history: [], busy: false, config: null, mode: "text", chat: null,
  audioURL: null, speaking: false, audioGeneration: 0, audioRequest: null,
};
const voice = {
  active: false, generation: 0, phase: "paused", status: null, context: null, output: null,
  stream: null, recorder: null, input: null, analyser: null, timer: null,
  permissionPending: false, request: null,
  inputLevel: 0,
};
const VOICE_SAMPLE_MS = 100;
const VOICE_SILENCE_MS = 3000;
const VOICE_NO_INPUT_MS = 30000;
const VOICE_MAX_MS = 120000;
const VOICE_MAX_BYTES = 4 * 1024 * 1024;
const VOICE_SIGNAL_THRESHOLD = 0.018;
const VOICE_PHASE_LABELS = {
  paused: ["pause", "잠시 멈췄어요"],
  starting: ["wait", "마이크를 준비하고 있어요"],
  permission: ["wait", "마이크 연결을 기다리고 있어요"],
  listening: ["mic", "듣고 있어요"],
  transcribing: ["wait", "말씀을 확인하고 있어요"],
  thinking: ["wait", "답변을 생각하고 있어요"],
  preparing: ["wait", "답변을 준비하고 있어요"],
  speaking: ["speaker", "온담이 이야기하고 있어요"],
};
const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
const initialMessages = $("messages").cloneNode(true);
let followConversation = true;
let activeSection = "#conversation";
$("today").textContent = new Intl.DateTimeFormat("ko-KR", {
  month: "long", day: "numeric", weekday: "long",
}).format(new Date());

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timeout = setTimeout(abort, 120000);
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin", ...options, signal: controller.signal,
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    if (controller.signal.aborted) throw new Error("답변이 지연되고 있어요. 잠시 후 다시 시도해 주세요.");
    throw new Error("연결을 확인해 주세요. 서버에 접속하지 못했습니다.");
  } finally {
    clearTimeout(timeout);
    // Keep request-scoped cancellation linked while callers await JSON or audio bodies.
    if (!response) options.signal?.removeEventListener("abort", abort);
  }
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : "요청을 처리하지 못했습니다.");
  }
  return response;
}

function jsonOptions(data) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) };
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function formatAnswer(container, text) {
  for (const part of text.split(/(\*\*[^*\n]+\*\*)/g)) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      const strong = document.createElement("strong");
      strong.textContent = part.slice(2, -2);
      container.append(strong);
    } else {
      container.append(document.createTextNode(part));
    }
  }
}

function setBusy(value) {
  state.busy = value;
  $("send").disabled = value;
  $("message").disabled = value;
  $("chat-form").setAttribute("aria-busy", String(value));
  updateVoiceControls();
}

function healthTopicCard(topic) {
  const card = document.createElement("details");
  card.className = "official-health-card";
  const summary = document.createElement("summary");
  summary.textContent = `공식 건강정보 · ${topic.title}`;
  const attribution = document.createElement("p");
  attribution.className = "health-attribution";
  attribution.textContent = "출처: 질병관리청 국가건강정보포털";
  const source = document.createElement("a");
  source.className = "source-link";
  source.textContent = "국가건강정보포털에서 전체 원문 보기";
  const url = new URL(topic.url);
  if (url.protocol !== "https:" || url.hostname !== "health.kdca.go.kr" || url.username || url.password) {
    throw new Error("공식 건강정보의 출처 주소를 확인하지 못했어요.");
  }
  source.href = url.href;
  source.target = "_blank";
  source.rel = "noopener noreferrer";
  const status = document.createElement("p");
  status.className = "health-load-status";
  status.setAttribute("role", "status");
  const content = document.createElement("div");
  content.className = "health-original";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "health-retry";
  retry.textContent = "다시 불러오기";
  retry.hidden = true;
  card.append(summary, attribution, source, status, content, retry);
  let loaded = false;
  let loading = false;
  async function load() {
    if (loaded || loading) return;
    loading = true;
    retry.hidden = true;
    status.setAttribute("role", "status");
    status.textContent = "공식 원문을 불러오고 있어요.";
    card.setAttribute("aria-busy", "true");
    try {
      const article = await (await api(`/api/health-info/${encodeURIComponent(topic.id)}`)).json();
      if (article.topic.id !== topic.id || !article.sections.length) {
        throw new Error("공식 건강정보의 내용을 확인하지 못했어요.");
      }
      const notice = document.createElement("p");
      notice.className = "health-original-notice";
      notice.textContent = `${article.notice}\n${article.license_notice}`;
      const fetched = document.createElement("p");
      fetched.className = "health-attribution";
      fetched.textContent = `조회 시각: ${new Date(article.fetched_at).toLocaleString("ko-KR")} (자료의 개정일은 아니에요)`;
      content.append(notice, fetched);
      article.sections.forEach((section) => {
        const part = document.createElement("details");
        part.className = "health-section";
        const heading = document.createElement("summary");
        heading.textContent = section.title;
        const text = document.createElement("p");
        text.textContent = section.text;
        part.append(heading, text);
        content.append(part);
      });
      status.textContent = "아래 항목을 펼쳐 원문 텍스트를 확인해 주세요.";
      loaded = true;
    } catch (error) {
      content.replaceChildren();
      status.setAttribute("role", "alert");
      status.textContent = error.message;
      retry.hidden = false;
    } finally {
      loading = false;
      card.setAttribute("aria-busy", "false");
    }
  }
  card.addEventListener("toggle", () => { if (card.open) void load(); });
  retry.addEventListener("click", () => { void load(); });
  return card;
}

function addMessage(role, text, result = null) {
  const article = document.createElement("article");
  article.className = `message ${role}${result?.mode === "emergency" ? " emergency" : ""}`;
  if (role === "assistant") {
    const avatar = document.createElement("img");
    avatar.src = "/static/mark.svg";
    avatar.alt = "";
    avatar.className = "avatar";
    article.append(avatar);
  }
  const body = document.createElement("div");
  body.className = "message-body";
  const name = document.createElement("div");
  name.className = "message-name";
  name.textContent = role === "user" ? "나의 이야기" : "온담";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") formatAnswer(bubble, text);
  else bubble.textContent = text;
  body.append(name, bubble);
  if (result) {
    if (result.sources.length) {
      const sources = document.createElement("div");
      sources.className = "sources";
      result.sources.forEach((source) => {
        let url;
        try { url = new URL(source.url); } catch { return; }
        if (url.protocol !== "https:" || url.username || url.password) return;
        const link = document.createElement("a");
        link.className = "source-link";
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = `[${source.id}] ${source.title}`;
        const provenance = source.evidence_kind === "dataset_knowledge"
          ? `\n수록 출처: ${source.source_display}\n자료 ID: ${source.record_id}\n기재 연도: ${source.source_year || "미상"}\n정제 확인: ${source.updated_at}`
          : source.evidence_kind === "official_api"
            ? `\n출처: ${source.source_display}\n콘텐츠 ID: ${source.record_id}\nAPI 조회 시각: ${source.updated_at} (자료 개정일은 아님)`
            : `\n자료 확인: ${source.updated_at}`;
        link.title = source.excerpt + provenance;
        link.append(icon("external"));
        sources.append(link);
      });
      body.append(sources);
    }
    const notice = document.createElement("div");
    notice.className = "message-notice";
    notice.textContent = result.notice;
    body.append(notice);
    if (result.medical_caution) {
      const caution = document.createElement("aside");
      caution.className = "medical-caution";
      caution.setAttribute("role", "note");
      const heading = document.createElement("strong");
      heading.textContent = "건강정보 이용 안내";
      const content = document.createElement("p");
      content.textContent = result.medical_caution;
      caution.append(heading, content);
      body.append(caution);
    }
    if (result.health_topics?.length) {
      const originals = document.createElement("aside");
      originals.className = "health-references";
      const heading = document.createElement("p");
      heading.textContent = "함께 확인할 공식 자료예요. 진단 결과나 개인별 권고는 아니에요.";
      originals.append(heading);
      result.health_topics.forEach((topic) => { originals.append(healthTopicCard(topic)); });
      body.append(originals);
    }
    if (state.config?.voice_enabled) {
      const speak = document.createElement("button");
      speak.type = "button";
      speak.className = "speak-action";
      speak.append(icon("speaker"), document.createTextNode("이 답변 듣기"));
      speak.addEventListener("click", () => { void speakText(text); });
      body.append(speak);
    }
  }
  article.append(body);
  $("messages").append(article);
  return article;
}

function scrollMessageIntoView(element, force = false) {
  if (!force && !followConversation) {
    $("new-answer").hidden = false;
    return;
  }
  const scroller = $("conversation-scroll");
  scroller.scrollTo({ top: scroller.scrollHeight, behavior: "instant" });
  followConversation = true;
  $("new-answer").hidden = true;
}

$("conversation-scroll").addEventListener("scroll", () => {
  const scroller = $("conversation-scroll");
  followConversation = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 72;
  if (followConversation) $("new-answer").hidden = true;
}, { passive: true });
$("new-answer").addEventListener("click", () => {
  scrollMessageIntoView($("messages").lastElementChild, true);
});

function updateAppViewport() {
  const viewport = globalThis.visualViewport;
  if (!viewport) return;
  document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
  document.documentElement.style.setProperty("--app-top", `${viewport.offsetTop}px`);
  document.documentElement.setAttribute(
    "data-viewport-size", viewport.height < 300 ? "tiny" : viewport.height <= 550 ? "compact" : "regular"
  );
}
globalThis.visualViewport?.addEventListener("resize", updateAppViewport, { passive: true });
globalThis.visualViewport?.addEventListener("scroll", updateAppViewport, { passive: true });
updateAppViewport();

function setActiveSection(href) {
  activeSection = href;
  $("current-section-title").textContent = href === "#topics" ? "함께 알아보기"
    : href === "#screen-settings" ? "화면 설정" : "온담과 대화하기";
  $("topics-title").textContent = href === "#topics" ? "함께 알아보기" : "어떤 이야기를 나눠볼까요?";
  document.querySelectorAll(".section-nav .nav-item").forEach((link) => {
    const active = link.getAttribute("href") === href;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
}

function showTopics(focus = true) {
  if (!$("health-library").hidden) $("health-library").open = true;
  const scroller = $("conversation-scroll");
  const target = $("topics");
  const top = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top
    + scroller.scrollTop - 20;
  scroller.scrollTo({ top, behavior: "instant" });
  if (focus) {
    target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }
}

function focusComposerUnlessBrowsing() {
  if (state.mode === "voice" || !followConversation) return;
  if (document.activeElement?.closest(".sidebar, .right-panel, #topics, dialog")) return;
  $("message").focus({ preventScroll: true });
}

document.querySelectorAll(".section-nav .nav-item").forEach((link) => {
  link.addEventListener("click", (event) => {
    if (link.classList.contains("settings-trigger")) return;
    event.preventDefault();
    const href = link.getAttribute("href");
    setActiveSection(href);
    history.replaceState(null, "", href);
    if (href === "#conversation") {
      scrollMessageIntoView($("messages").lastElementChild, true);
      if (!state.busy && state.mode === "text") $("message").focus({ preventScroll: true });
    } else {
      showTopics();
    }
  });
});
for (const [id, href] of [
  ["topics", "#topics"],
  ["chat-form", "#conversation"], ["messages", "#conversation"],
]) {
  $(id).addEventListener("focusin", () => setActiveSection(href));
}
setActiveSection(
  location.hash === "#topics" ? "#topics" : "#conversation"
);
if (location.hash === "#topics") showTopics(false);

async function sendMessage(text, voiceToken = null) {
  if (state.busy || !text.trim()) return;
  if (!state.config) { showError("서비스 연결을 확인하고 있어요. 새로고침 후 다시 시도해 주세요."); return; }
  const value = text.trim();
  if (value.length > 2000) { showError("메시지는 2,000자 이하로 적어 주세요."); return; }
  if (voiceToken === null && state.mode === "voice" && voice.active) {
    if (["permission", "transcribing", "thinking", "starting"].includes(voice.phase)) return;
    cancelVoice("", false);
    voice.active = true;
    voiceToken = voice.generation;
    $("voice-transcript").textContent = value;
  }
  if (voiceToken !== null && !voiceCurrent(voiceToken)) return;
  stopAudio();
  showError("");
  if (voiceToken !== null) setVoicePhase("thinking", "말씀하신 내용에 답변을 준비하고 있어요.");
  setBusy(true);
  const userMessage = addMessage("user", value);
  $("message").value = "";
  $("message").style.height = "";
  const loading = $("loading-template").content.cloneNode(true);
  $("messages").append(loading);
  const loadingMessage = $("messages").lastElementChild;
  const chat = { controller: new AbortController(), userMessage, loadingMessage, voiceToken, value };
  state.chat = chat;
  scrollMessageIntoView($("messages").lastElementChild, true);
  let result;
  try {
    const response = await api("/api/chat", {
      ...jsonOptions({ message: value, history: state.history }), signal: chat.controller.signal,
    });
    result = await response.json();
    if (state.chat !== chat || (voiceToken !== null && !voiceCurrent(voiceToken))) return;
    loadingMessage.remove();
    const article = addMessage("assistant", result.answer, result);
    state.history.push({ role: "user", content: value }, {
      role: "assistant", content: result.answer.slice(0, 4000),
    });
    state.history = state.history.slice(-12);
    scrollMessageIntoView(article);
  } catch (error) {
    if (state.chat !== chat) return;
    userMessage.remove();
    $("message").value = value;
    showError(error.message);
    if (voiceToken !== null) pauseVoice(`답변을 받지 못해 잠시 멈췄어요. ${error.message}`);
  } finally {
    loadingMessage.remove();
    if (state.chat === chat) {
      state.chat = null;
      setBusy(false);
      focusComposerUnlessBrowsing();
    }
  }
  if (result && voiceToken !== null && voiceCurrent(voiceToken)) {
    await speakVoiceAnswer(result, voiceToken);
  }
}

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void sendMessage($("message").value);
});
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendMessage($("message").value);
  }
});
$("message").addEventListener("input", () => {
  $("message").style.height = "auto";
  $("message").style.height = `${Math.min($("message").scrollHeight, 160)}px`;
});
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => { void sendMessage(button.dataset.prompt); });
});

function stopAudio() {
  state.audioGeneration += 1;
  state.audioRequest?.abort();
  state.audioRequest = null;
  state.speaking = false;
  $("audio").pause();
  $("audio").removeAttribute("src");
  $("audio").load();
  if (state.audioURL) URL.revokeObjectURL(state.audioURL);
  state.audioURL = null;
  $("audio-panel").hidden = true;
  document.querySelectorAll(".speak-action").forEach((button) => { button.disabled = false; });
}

async function speakText(text) {
  if (state.speaking) return;
  if (voice.active) pauseVoice("답변을 듣는 동안 마이크는 꺼져 있어요. 다시 말씀하시려면 대화 시작을 눌러 주세요.");
  stopAudio();
  const generation = state.audioGeneration;
  const controller = new AbortController();
  state.audioRequest = controller;
  state.speaking = true;
  document.querySelectorAll(".speak-action").forEach((button) => { button.disabled = true; });
  try {
    const response = await api("/api/speech/synthesize", {
      ...jsonOptions({ text: speechText(text) }), signal: controller.signal,
    });
    if (generation !== state.audioGeneration) return;
    const blob = await response.blob();
    if (generation !== state.audioGeneration) return;
    state.audioURL = URL.createObjectURL(blob);
    $("audio").src = state.audioURL;
    $("audio-panel").hidden = false;
    try { await $("audio").play(); }
    catch { showError("음성이 준비됐어요. 재생 버튼을 눌러 들어 주세요."); }
  } catch (error) { if (generation === state.audioGeneration) showError(error.message); }
  finally {
    if (generation === state.audioGeneration) {
      state.audioRequest = null;
      state.speaking = false;
      document.querySelectorAll(".speak-action").forEach((button) => { button.disabled = false; });
    }
  }
}
$("close-audio").addEventListener("click", stopAudio);

function speechText(text) {
  return text.slice(0, 4000);
}

function voiceType() {
  if (!globalThis.MediaRecorder?.isTypeSupported) return null;
  return ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"]
    .find((type) => MediaRecorder.isTypeSupported(type));
}

function voiceAvailable() {
  return state.config?.voice_enabled && navigator.mediaDevices?.getUserMedia
    && AudioContextClass && voiceType();
}

function voiceCurrent(token) {
  return voice.active && state.mode === "voice" && token === voice.generation;
}

function updateVoiceControls() {
  if (voice.status !== null) {
    $("voice-status").textContent = voice.status
      + (voice.phase === "paused" && voice.permissionPending
        ? " 열려 있는 마이크 허용 창을 먼저 닫거나 선택해 주세요." : "");
  }
  const [iconName, label] = VOICE_PHASE_LABELS[voice.phase];
  const listening = voice.phase === "listening";
  const interruptible = voice.active && ["speaking", "preparing"].includes(voice.phase);
  $("voice-panel").setAttribute("data-voice-phase", voice.phase);
  $("voice-state-icon").setAttribute("href", `#i-${iconName}`);
  $("voice-state-label").textContent = label;
  $("voice-input-feedback").hidden = !listening;
  $("voice-processing").hidden = !["starting", "permission", "transcribing", "thinking", "preparing"].includes(voice.phase);
  if (!listening) setVoiceInputLevel(0);
  $("mode-voice").disabled = !voiceAvailable();
  $("voice-start").hidden = voice.active && !interruptible;
  $("voice-start").textContent = interruptible ? "답변 멈추고 말하기" : "대화 시작";
  $("voice-start").disabled = !voiceAvailable() || state.busy || voice.permissionPending
    || (voice.active && voice.phase !== "speaking" && voice.phase !== "preparing");
  $("voice-finish").hidden = !listening;
  $("voice-finish").disabled = voice.phase !== "listening";
  $("voice-pause").hidden = !voice.active;
  $("voice-pause").disabled = !voice.active;
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.disabled = state.busy || (voice.active
      && ["starting", "permission", "transcribing"].includes(voice.phase));
  });
}

function setVoiceInputLevel(rms) {
  const level = voice.phase === "listening" && voice.active && voice.stream
    && rms >= VOICE_SIGNAL_THRESHOLD ? Math.min(5, Math.ceil(rms / 0.025)) : 0;
  voice.inputLevel = level;
  if ($("voice-input-meter").getAttribute("data-level") !== String(level)) {
    $("voice-input-meter").setAttribute("data-level", String(level));
  }
  const label = level > 0 ? "소리가 들어오고 있어요" : "편하게 말씀해 주세요";
  if ($("voice-input-label").textContent !== label) $("voice-input-label").textContent = label;
}

function setVoicePhase(phase, message) {
  voice.phase = phase;
  voice.status = message;
  updateVoiceControls();
}

function releaseMicrophone() {
  clearTimeout(voice.timer);
  voice.timer = null;
  voice.input?.disconnect();
  voice.analyser?.disconnect();
  voice.input = null;
  voice.analyser = null;
  voice.stream?.getTracks().forEach((track) => track.stop());
  voice.stream = null;
  setVoiceInputLevel(0);
}

function stopVoiceOutput() {
  if (!voice.output) return;
  voice.output.onended = null;
  voice.output.stop();
  voice.output.disconnect();
  voice.output = null;
}

function cancelChat(voiceOnly = false) {
  if (!state.chat || (voiceOnly && state.chat.voiceToken === null)) return;
  const chat = state.chat;
  state.chat = null;
  chat.controller.abort();
  chat.userMessage.remove();
  chat.loadingMessage.remove();
  $("message").value = chat.value;
  setBusy(false);
}

function cancelVoice(message, closeContext = true) {
  voice.generation += 1;
  voice.active = false;
  voice.request?.abort();
  voice.request = null;
  cancelChat(true);
  const recorder = voice.recorder;
  voice.recorder = null;
  releaseMicrophone();
  if (recorder?.state === "recording") recorder.stop();
  stopVoiceOutput();
  if (closeContext && voice.context) {
    void voice.context.close().catch(() => {});
    voice.context = null;
  }
  setVoicePhase("paused", message || "음성 대화를 잠시 멈췄어요. 대화 시작을 누르면 다시 들을게요.");
}

function pauseVoice(message) {
  cancelVoice(message);
}

function selectMode(mode) {
  if (mode === "voice" && !voiceAvailable()) return;
  if (mode === state.mode) {
    if (mode === "voice" && !voice.active) void startVoice();
    return;
  }
  pauseVoice();
  if (mode === "voice") cancelChat();
  stopAudio();
  state.mode = mode;
  document.body.classList.toggle("voice-mode", mode === "voice");
  $("mode-text").setAttribute("aria-pressed", String(mode === "text"));
  $("mode-voice").setAttribute("aria-pressed", String(mode === "voice"));
  $("voice-panel").hidden = mode !== "voice";
  $("chat-form").hidden = mode === "voice";
  $("composer-hint").hidden = mode === "voice";
  $("mode-description").textContent = mode === "voice"
    ? "말씀을 마치고 3초 쉬면 답변해요. 답변을 다 들은 뒤 다시 말씀해 주세요."
    : "글로 적어 보내 주세요. 답변의 듣기 버튼으로 음성을 들을 수 있어요.";
  if (mode === "voice") {
    $("message").blur();
    void startVoice();
  } else {
    focusComposerUnlessBrowsing();
  }
}

async function startVoice() {
  if (!voiceAvailable() || state.mode !== "voice" || document.hidden
      || state.busy || voice.permissionPending) return;
  if (voice.active && !["speaking", "preparing"].includes(voice.phase)) return;
  cancelVoice("", false);
  stopAudio();
  voice.active = true;
  const token = voice.generation;
  setVoicePhase("starting", "마이크를 준비하고 있어요.");
  showError("");
  try {
    // Resuming under this explicit click also unlocks subsequent iOS Web Audio replies.
    voice.context ||= new AudioContextClass();
    await voice.context.resume();
    if (!voiceCurrent(token)) return;
    if (voice.context.state !== "running") throw new Error("소리를 시작하지 못했어요. 대화 시작을 다시 눌러 주세요.");
    await listenForTurn(token);
  } catch (error) {
    if (voiceCurrent(token)) failVoice(error);
  }
}

function failVoice(error) {
  const message = error.name === "NotAllowedError"
    ? "마이크 사용이 허용되지 않았어요. 브라우저 사이트 설정에서 허용하거나 글로 대화해 주세요."
    : error.message || "음성 연결에 문제가 생겼어요. 다시 시작하거나 글로 대화해 주세요.";
  pauseVoice(`잠시 멈췄어요. ${message}`);
  showError(message);
}

async function listenForTurn(token) {
  if (!voiceCurrent(token) || state.busy || voice.permissionPending || voice.recorder) return;
  setVoicePhase("permission", "마이크 사용을 확인하고 있어요.");
  voice.permissionPending = true;
  updateVoiceControls();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!voiceCurrent(token)) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    voice.stream = stream;
    const context = voice.context;
    voice.analyser = context.createAnalyser();
    voice.analyser.fftSize = 2048;
    voice.input = context.createMediaStreamSource(stream);
    voice.input.connect(voice.analyser);
    const type = voiceType();
    const recorder = new MediaRecorder(stream, { mimeType: type });
    voice.recorder = recorder;
    const chunks = [];
    let bytes = 0;
    recorder.addEventListener("dataavailable", (event) => {
      if (!voiceCurrent(token) || voice.recorder !== recorder || !event.data.size) return;
      if (bytes + event.data.size > VOICE_MAX_BYTES) {
        failVoice(new Error("음성 크기가 처리 한도(4MB)를 넘어 전송하지 않았어요. 나누어 말씀하거나 글로 적어 주세요."));
        return;
      }
      bytes += event.data.size;
      chunks.push(event.data);
    });
    recorder.addEventListener("error", () => {
      if (voiceCurrent(token)) failVoice(new Error("말씀을 듣는 중 문제가 생겼어요. 마이크 연결을 확인해 주세요."));
    });
    recorder.addEventListener("stop", () => {
      if (voice.recorder !== recorder) return;
      voice.recorder = null;
      if (voiceCurrent(token) && voice.phase === "transcribing") {
        void transcribeTurn(new Blob(chunks, { type }), token);
      } else if (voiceCurrent(token)) {
        failVoice(new Error("마이크 연결이 중단됐어요. 대화 시작을 다시 눌러 주세요."));
      }
    });
    recorder.start(1000);
    setVoicePhase("listening", "듣고 있어요. 말씀을 마치고 3초 쉬거나 말 마침을 눌러 주세요.");
    const samples = new Float32Array(voice.analyser.fftSize);
    const started = Date.now();
    let speechMs = 0;
    let heardSpeech = false;
    let lastSpeech = started;
    const sample = () => {
      if (!voiceCurrent(token) || voice.phase !== "listening") return;
      voice.analyser.getFloatTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
      setVoiceInputLevel(rms);
      const now = Date.now();
      if (rms >= VOICE_SIGNAL_THRESHOLD) {
        speechMs += VOICE_SAMPLE_MS;
        if (speechMs >= 300) heardSpeech = true;
        if (heardSpeech) lastSpeech = now;
      } else if (!heardSpeech) speechMs = 0;
      if (now - started >= VOICE_MAX_MS) {
        $("voice-caption").textContent = "한 번에 최대 2분까지 말씀하실 수 있어요. 지금까지 말씀하신 내용에 답한 뒤 다시 들을게요.";
        finishVoiceTurn();
      } else if (!heardSpeech && now - started >= VOICE_NO_INPUT_MS) {
        pauseVoice("30초 동안 말씀이 없어 잠시 멈췄어요. 준비되시면 대화 시작을 눌러 주세요.");
      } else if (heardSpeech && now - lastSpeech >= VOICE_SILENCE_MS) {
        finishVoiceTurn();
      } else {
        voice.timer = setTimeout(sample, VOICE_SAMPLE_MS);
      }
    };
    voice.timer = setTimeout(sample, VOICE_SAMPLE_MS);
  } catch (error) {
    if (voiceCurrent(token)) failVoice(error);
  } finally {
    voice.permissionPending = false;
    updateVoiceControls();
  }
}

function finishVoiceTurn() {
  if (!voice.active || voice.phase !== "listening") return;
  setVoicePhase("transcribing", "마이크를 끄고 말씀하신 내용을 확인하고 있어요.");
  releaseMicrophone();
  if (voice.recorder?.state === "recording") voice.recorder.stop();
}

async function voiceJSON(path, options) {
  const controller = new AbortController();
  voice.request = controller;
  try {
    const response = await api(path, { ...options, signal: controller.signal });
    return await response.json();
  } finally {
    if (voice.request === controller) voice.request = null;
  }
}

async function transcribeTurn(blob, token) {
  try {
    if (!blob.size || blob.size > VOICE_MAX_BYTES) {
      throw new Error("말씀하신 음성을 처리하지 못했어요. 다시 말씀하거나 글로 적어 주세요.");
    }
    const form = new FormData();
    form.append("audio", blob, "recording");
    const data = await voiceJSON("/api/speech/transcribe", { method: "POST", body: form });
    if (!voiceCurrent(token)) return;
    if (typeof data.text !== "string" || !data.text.trim()) {
      throw new Error("말씀을 알아듣지 못했어요. 다시 시작하거나 글로 적어 주세요.");
    }
    const text = data.text.trim();
    $("voice-transcript").textContent = text;
    if (text.length > 2000) {
      $("message").value = text;
      throw new Error("인식된 말씀이 2,000자를 넘었어요. 내용을 자르지 않았으니 글로 전환해 고쳐 보내 주세요.");
    }
    await sendMessage(text, token);
  } catch (error) {
    if (voiceCurrent(token)) failVoice(error);
  }
}

async function speakVoiceAnswer(result, token) {
  $("voice-caption").textContent = result.answer;
  setVoicePhase("preparing", "답변을 소리로 준비하고 있어요. 마이크는 꺼져 있어요.");
  const controller = new AbortController();
  voice.request = controller;
  try {
    const response = await api("/api/speech/synthesize", {
      ...jsonOptions({ text: speechText(result.answer) }), signal: controller.signal,
    });
    if (!voiceCurrent(token)) return;
    const blob = await response.blob();
    if (!voiceCurrent(token)) return;
    const data = await blob.arrayBuffer();
    if (!voiceCurrent(token)) return;
    const context = voice.context;
    const buffer = await context.decodeAudioData(data);
    if (!voiceCurrent(token) || voice.context !== context) return;
    if (context.state !== "running") throw new Error("소리가 일시 중지됐어요. 대화 시작을 다시 눌러 주세요.");
    const output = context.createBufferSource();
    output.buffer = buffer;
    output.connect(context.destination);
    voice.output = output;
    output.onended = () => {
      if (!voiceCurrent(token) || voice.output !== output) return;
      output.disconnect();
      voice.output = null;
      if (result.mode === "emergency") {
        pauseVoice("안전을 위해 자동 대화를 멈췄어요. 위 안내를 확인하고 필요한 경우 119에 연락해 주세요.");
      } else {
        void listenForTurn(token);
      }
    };
    setVoicePhase("speaking", "마이크는 꺼져 있어요. 지금 말씀하시려면 ‘답변 멈추고 말하기’를 눌러 주세요.");
    output.start();
  } catch (error) {
    if (voiceCurrent(token)) failVoice(new Error(`답변 글은 남아 있어요. 음성 재생을 멈췄어요. ${error.message}`));
  } finally {
    if (voice.request === controller) voice.request = null;
  }
}

$("mode-voice").disabled = true;
$("mode-text").addEventListener("click", () => selectMode("text"));
$("mode-voice").addEventListener("click", () => selectMode("voice"));
$("voice-start").addEventListener("click", () => { void startVoice(); });
$("voice-finish").addEventListener("click", finishVoiceTurn);
$("voice-pause").addEventListener("click", () => pauseVoice());
$("new-chat").addEventListener("click", () => {
  stopAudio();
  pauseVoice("새 대화를 시작했어요. 대화 시작을 누르면 들을게요.");
  cancelChat();
  state.history = [];
  $("messages").replaceChildren(...initialMessages.cloneNode(true).childNodes);
  $("message").value = "";
  $("voice-transcript").textContent = "";
  $("voice-caption").textContent = "";
  $("message").style.height = "";
  $("new-answer").hidden = true;
  showError("");
  setActiveSection("#conversation");
  history.replaceState(null, "", location.pathname + location.search);
  $("conversation-scroll").scrollTo({ top: 0, behavior: "instant" });
  followConversation = true;
});
for (const size of ["normal", "large"]) {
  $(`font-${size}`).addEventListener("click", () => {
    document.body.classList.toggle("large-text", size === "large");
    for (const candidate of ["normal", "large"]) {
      $(`font-${candidate}`).classList.toggle("selected", candidate === size);
      $(`font-${candidate}`).setAttribute("aria-pressed", String(candidate === size));
    }
  });
}

function suspendAudio() {
  pauseVoice("화면을 벗어나 음성 대화를 멈췄어요. 대화 시작을 눌러 다시 이어가 주세요.");
  stopAudio();
}
addEventListener("pagehide", suspendAudio);
document.addEventListener("visibilitychange", () => { if (document.hidden) suspendAudio(); });
$("new-chat-mobile").addEventListener("click", () => $("new-chat").click());
function bindDialog(openId, closeId, dialogId, openOnClick = true) {
  const dialog = $(dialogId);
  if (openOnClick) {
    $(openId).addEventListener("click", (event) => {
      event.preventDefault();
      if (voice.active) pauseVoice("안내창을 열어 잠시 멈췄어요. 닫은 뒤 대화 시작을 눌러 주세요.");
      stopAudio();
      dialog.showModal();
    });
  }
  $(closeId).addEventListener("click", () => dialog.close());
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      dialog.close();
    }
  });
}
bindDialog("open-info", "close-info", "info-dialog");
let settingsOpener = null;
let settingsPreviousSection = "#conversation";
function openScreenSettings(opener) {
  if ($("screen-settings").open) return;
  settingsOpener = opener;
  settingsPreviousSection = activeSection;
  if (voice.active) pauseVoice("화면 설정을 열어 잠시 멈췄어요. 닫은 뒤 대화 시작을 눌러 주세요.");
  stopAudio();
  setActiveSection("#screen-settings");
  history.replaceState(null, "", "#screen-settings");
  $("screen-settings").showModal();
}
document.querySelectorAll(".settings-trigger").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    openScreenSettings(button);
  });
});
$("close-settings").addEventListener("click", () => $("screen-settings").close());
$("screen-settings").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    $("screen-settings").close();
  }
});
$("screen-settings").addEventListener("close", () => {
  setActiveSection(settingsPreviousSection);
  history.replaceState(null, "", settingsPreviousSection);
  settingsOpener?.focus({ preventScroll: true });
});
if (["#screen-settings", "#voice-settings"].includes(location.hash)) {
  openScreenSettings(document.querySelector(
    matchMedia("(max-width: 700px)").matches ? ".topbar-settings" : ".section-nav .settings-trigger"
  ));
}
addEventListener("hashchange", () => {
  if (location.hash === "#topics") {
    setActiveSection("#topics");
    showTopics(false);
  } else if (location.hash === "#conversation") {
    setActiveSection("#conversation");
    scrollMessageIntoView($("messages").lastElementChild, true);
  }
});
const mobileTelephoneLink = navigator.userAgentData?.mobile === true
  || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
if (!mobileTelephoneLink) {
  for (const id of ["open-emergency", "open-emergency-compact"]) {
    $(id).setAttribute("aria-haspopup", "dialog");
    $(id).setAttribute("aria-controls", "emergency-dialog");
  }
  $("emergency-hint").textContent = "전화 연결 방법을 확인해요";
}
bindDialog("open-emergency", "close-emergency", "emergency-dialog", !mobileTelephoneLink);
if (!mobileTelephoneLink) {
  $("open-emergency-compact").addEventListener("click", (event) => {
    event.preventDefault();
    if (voice.active) pauseVoice("119 안내를 확인하는 동안 잠시 멈췄어요.");
    stopAudio();
    $("emergency-dialog").showModal();
  });
}

let healthTopics = [];
$("open-health-topic").addEventListener("click", () => {
  const topic = healthTopics.find((item) => item.id === $("health-topic").value);
  if (!topic) { showError("건강정보 주제를 선택해 주세요."); return; }
  const card = healthTopicCard(topic);
  $("health-library-result").replaceChildren(card);
  card.open = true;
});

async function initializeHealthLibrary() {
  if (!state.config.health_info_enabled) return;
  try {
    healthTopics = await (await api("/api/health-topics")).json();
    healthTopics.forEach((topic) => {
      const option = document.createElement("option");
      option.value = topic.id;
      option.textContent = topic.title;
      $("health-topic").append(option);
    });
    $("health-library").hidden = false;
    if (activeSection === "#topics") showTopics(false);
  } catch (error) {
    showError(`공식 건강정보 목록을 불러오지 못했어요. ${error.message}`);
  }
}

async function initialize() {
  try {
    state.config = await (await api("/api/config")).json();
    $("status").lastChild.textContent = state.config.search_enabled ? "상담 준비 완료" : "일반 대화 모드";
    updateVoiceControls();
    if (!voiceAvailable()) {
      $("mode-description").textContent = "이 환경에서는 음성 대화를 사용할 수 없어요. 글로 대화해 주세요.";
      $("mode-voice").title = "마이크와 음성 재생을 지원하는 브라우저에서 이용해 주세요.";
    }
    await initializeHealthLibrary();
  } catch (error) {
    $("status").classList.add("unavailable");
    $("status").lastChild.textContent = "연결 확인 필요";
    showError(error.message);
  }
}
void initialize();
