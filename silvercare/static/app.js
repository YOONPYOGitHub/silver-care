"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  history: [], busy: false, config: null, recorder: null, stream: null,
  recordingTimer: null, discardRecording: false, audioURL: null, speaking: false, audioGeneration: 0,
  recordingGeneration: 0, recordingPending: false,
};
const initialMessages = $("messages").cloneNode(true);
$("today").textContent = new Intl.DateTimeFormat("ko-KR", {
  month: "long", day: "numeric", weekday: "long",
}).format(new Date());

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin", signal: AbortSignal.timeout(120000), ...options,
    });
  } catch (error) {
    if (error.name === "TimeoutError") throw new Error("답변이 지연되고 있어요. 잠시 후 다시 시도해 주세요.");
    throw new Error("연결을 확인해 주세요. 서버에 접속하지 못했습니다.");
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
  $("new-chat").disabled = value;
  $("new-chat-mobile").disabled = value;
  $("record").disabled = value || state.recordingPending || !$("stt-toggle").checked;
  document.querySelectorAll("[data-prompt]").forEach((button) => { button.disabled = value; });
  $("chat-form").setAttribute("aria-busy", String(value));
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

function scrollMessageIntoView(element) {
  element.scrollIntoView({
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    block: "nearest",
  });
}

function setActiveSection(href) {
  document.querySelectorAll(".section-nav .nav-item").forEach((link) => {
    const active = link.getAttribute("href") === href;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
}

function focusComposerUnlessBrowsing() {
  if (document.activeElement?.closest(".sidebar, .right-panel, #topics, dialog")) return;
  $("message").focus({ preventScroll: true });
}

document.querySelectorAll(".section-nav .nav-item").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    const href = link.getAttribute("href");
    const target = document.querySelector(href);
    setActiveSection(href);
    history.replaceState(null, "", href);
    const behavior = matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
    if (href === "#conversation") {
      $("chat-form").scrollIntoView({ behavior, block: "nearest" });
      if (!state.busy) $("message").focus({ preventScroll: true });
    } else {
      target.scrollIntoView({ behavior, block: "nearest" });
      const control = target.querySelector("button:not(:disabled), input:not(:disabled)");
      if (control) control.focus({ preventScroll: true });
      else {
        target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: true });
      }
    }
  });
});
for (const [id, href] of [
  ["topics", "#topics"], ["voice-settings", "#voice-settings"],
  ["chat-form", "#conversation"], ["messages", "#conversation"],
]) {
  $(id).addEventListener("focusin", () => setActiveSection(href));
}
setActiveSection(
  ["#topics", "#voice-settings"].includes(location.hash) ? location.hash : "#conversation"
);

async function sendMessage(text) {
  if (state.busy || !text.trim()) return;
  if (!state.config) { showError("서비스 연결을 확인하고 있어요. 새로고침 후 다시 시도해 주세요."); return; }
  if (state.recorder) stopRecording(true);
  stopAudio();
  showError("");
  const value = text.trim();
  if (value.length > 2000) { showError("메시지는 2,000자 이하로 적어 주세요."); return; }
  setBusy(true);
  const userMessage = addMessage("user", value);
  $("message").value = "";
  $("message").style.height = "";
  const loading = $("loading-template").content.cloneNode(true);
  $("messages").append(loading);
  scrollMessageIntoView($("messages").lastElementChild);
  try {
    const response = await api("/api/chat", jsonOptions({ message: value, history: state.history }));
    const result = await response.json();
    document.querySelector(".loading")?.remove();
    const article = addMessage("assistant", result.answer, result);
    state.history.push({ role: "user", content: value }, {
      role: "assistant", content: result.answer.slice(0, 4000),
    });
    state.history = state.history.slice(-12);
    scrollMessageIntoView(article);
    if ($("tts-toggle").checked) void speakText(result.answer);
  } catch (error) {
    userMessage.remove();
    $("message").value = value;
    showError(error.message);
  } finally {
    document.querySelector(".loading")?.remove();
    setBusy(false);
    focusComposerUnlessBrowsing();
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
  $("audio").pause();
  $("audio").removeAttribute("src");
  $("audio").load();
  if (state.audioURL) URL.revokeObjectURL(state.audioURL);
  state.audioURL = null;
  $("audio-panel").hidden = true;
}

async function speakText(text) {
  if (state.speaking) return;
  stopAudio();
  const generation = state.audioGeneration;
  state.speaking = true;
  document.querySelectorAll(".speak-action").forEach((button) => { button.disabled = true; });
  try {
    const response = await api("/api/speech/synthesize", jsonOptions({
      text: text.replace(/\[\d+\]/g, "").slice(0, 4000),
    }));
    if (generation !== state.audioGeneration) return;
    const blob = await response.blob();
    if (generation !== state.audioGeneration) return;
    state.audioURL = URL.createObjectURL(blob);
    $("audio").src = state.audioURL;
    $("audio-panel").hidden = false;
    try { await $("audio").play(); }
    catch { showError("음성이 준비됐어요. 재생 버튼을 눌러 들어 주세요."); }
  } catch (error) { showError(error.message); }
  finally {
    state.speaking = false;
    document.querySelectorAll(".speak-action").forEach((button) => { button.disabled = false; });
  }
}
$("close-audio").addEventListener("click", stopAudio);
$("tts-toggle").addEventListener("change", () => {
  if (!$("tts-toggle").checked) stopAudio();
});

function stopRecording(discard = false) {
  state.recordingGeneration += 1;
  state.discardRecording = discard;
  clearTimeout(state.recordingTimer);
  if (state.recorder?.state === "recording") state.recorder.stop();
  state.stream?.getTracks().forEach((track) => track.stop());
  state.stream = null;
  $("record").classList.remove("recording");
  $("record").setAttribute("aria-label", "음성으로 입력하기");
  $("composer-hint").textContent = "이름, 연락처, 주민등록번호는 적지 말아 주세요.";
}

async function startRecording() {
  if (state.recordingPending || state.busy || !$("stt-toggle").checked) return;
  if (state.recorder?.state === "recording") { stopRecording(); return; }
  if (!navigator.mediaDevices?.getUserMedia || !globalThis.MediaRecorder) {
    showError("이 브라우저는 녹음을 지원하지 않아요. HTTPS 환경의 최신 Chrome 또는 Edge를 이용해 주세요.");
    return;
  }
  showError("");
  stopAudio();
  const generation = state.recordingGeneration;
  try {
    const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"]
      .find((candidate) => MediaRecorder.isTypeSupported(candidate));
    if (!type) throw new Error("이 브라우저의 녹음 형식은 지원하지 않아요. 최신 Chrome 또는 Edge를 이용해 주세요.");
    state.recordingPending = true;
    $("record").disabled = true;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (generation !== state.recordingGeneration || !$("stt-toggle").checked || state.busy) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    state.stream = stream;
    state.discardRecording = false;
    const recorder = new MediaRecorder(state.stream, { mimeType: type });
    state.recorder = recorder;
    const chunks = [];
    recorder.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener("error", () => {
      stopRecording(true);
      showError("녹음 중 문제가 생겼어요. 마이크 연결을 확인해 주세요.");
    });
    recorder.addEventListener("stop", async () => {
      state.recorder = null;
      if (state.discardRecording) return;
      const blob = new Blob(chunks, { type });
      if (!blob.size || blob.size > 4 * 1024 * 1024) {
        showError("녹음이 비어 있거나 너무 커요. 짧게 다시 말씀해 주세요.");
        return;
      }
      const form = new FormData();
      form.append("audio", blob, "recording");
      setBusy(true);
      try {
        const response = await api("/api/speech/transcribe", { method: "POST", body: form });
        const data = await response.json();
        $("message").value = data.text.slice(0, 2000);
        $("message").dispatchEvent(new Event("input"));
        $("composer-hint").textContent = "인식된 내용을 확인한 뒤 보내기 버튼을 눌러 주세요.";
      } catch (error) { showError(error.message); }
      finally { setBusy(false); focusComposerUnlessBrowsing(); }
    });
    recorder.start();
    $("record").classList.add("recording");
    $("record").setAttribute("aria-label", "녹음 중지하고 음성 인식하기");
    $("composer-hint").textContent = "듣고 있어요. 마이크를 다시 누르면 완료돼요. (최대 45초)";
    state.recordingTimer = setTimeout(() => stopRecording(), 45000);
  } catch (error) {
    state.stream?.getTracks().forEach((track) => track.stop());
    state.stream = null;
    if (generation !== state.recordingGeneration) return;
    showError(error.name === "NotAllowedError" ? "마이크 사용을 허용해 주세요. 브라우저의 사이트 설정에서 바꿀 수 있어요." : error.message);
  } finally {
    state.recordingPending = false;
    $("record").disabled = state.busy || !$("stt-toggle").checked;
  }
}
$("record").addEventListener("click", () => { void startRecording(); });
$("stt-toggle").addEventListener("change", () => {
  $("record").disabled = !$("stt-toggle").checked || state.busy || state.recordingPending;
  $("record").title = $("stt-toggle").checked ? "눌러서 녹음 시작 · 다시 눌러서 완료" : "설정에서 음성 입력을 켜 주세요";
  if (!$("stt-toggle").checked) stopRecording(true);
});
$("new-chat").addEventListener("click", () => {
  if (state.busy) return;
  stopAudio();
  stopRecording(true);
  state.history = [];
  $("messages").replaceChildren(...initialMessages.cloneNode(true).childNodes);
  $("message").value = "";
  showError("");
  setActiveSection("#conversation");
  history.replaceState(null, "", location.pathname + location.search);
  $("welcome").scrollIntoView({ block: "start" });
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

addEventListener("pagehide", () => { stopRecording(true); stopAudio(); });
$("new-chat-mobile").addEventListener("click", () => $("new-chat").click());
function bindDialog(openId, closeId, dialogId, openOnClick = true) {
  const dialog = $(dialogId);
  if (openOnClick) {
    $(openId).addEventListener("click", (event) => {
      event.preventDefault();
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
const mobileTelephoneLink = navigator.userAgentData?.mobile === true
  || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
if (!mobileTelephoneLink) {
  $("open-emergency").setAttribute("aria-haspopup", "dialog");
  $("open-emergency").setAttribute("aria-controls", "emergency-dialog");
  $("emergency-hint").textContent = "전화 연결 방법을 확인해요";
}
bindDialog("open-emergency", "close-emergency", "emergency-dialog", !mobileTelephoneLink);

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
  } catch (error) {
    showError(`공식 건강정보 목록을 불러오지 못했어요. ${error.message}`);
  }
}

async function initialize() {
  try {
    state.config = await (await api("/api/config")).json();
    $("status").lastChild.textContent = state.config.search_enabled ? "상담 준비 완료" : "일반 대화 모드";
    $("stt-toggle").disabled = !state.config.voice_enabled;
    $("tts-toggle").disabled = !state.config.voice_enabled;
    if (!state.config.voice_enabled) {
      document.querySelector(".voice-privacy span").textContent = "음성 서비스가 아직 설정되지 않았어요. 텍스트로 대화해 주세요.";
    }
    await initializeHealthLibrary();
  } catch (error) {
    $("status").classList.add("unavailable");
    $("status").lastChild.textContent = "연결 확인 필요";
    showError(error.message);
  }
}
void initialize();
