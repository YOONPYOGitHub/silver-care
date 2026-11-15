import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const flush = () => new Promise(setImmediate);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Element extends EventTarget {
  constructor(id = "") {
    super();
    this.id = id;
    this.disabled = false;
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    this.style = {};
    this.scrollTop = 0;
    this.scrollHeight = 1000;
    this.clientHeight = 500;
    this.scrollCalls = 0;
    this.open = false;
    this.childNodes = [];
    this.attributes = new Map();
    this.dataset = {};
    this.className = "";
    const classes = new Set();
    this.classList = {
      add: (value) => classes.add(value),
      remove: (value) => classes.delete(value),
      contains: (value) => classes.has(value),
      toggle(value, enabled) {
        if (enabled ?? !classes.has(value)) classes.add(value);
        else classes.delete(value);
      },
    };
  }
  cloneNode() {
    const clone = new Element(this.id);
    clone.className = this.className;
    clone.append(...this.childNodes.map((child) => child.cloneNode()));
    return clone;
  }
  append(...children) {
    for (const child of children) {
      child.parent = this;
      if (this.childContentTop !== undefined) child.contentTop = this.childContentTop;
      this.childNodes.push(child);
    }
  }
  get parentNode() { return this.parent ?? null; }
  get lastChild() { return this.childNodes.at(-1); }
  get lastElementChild() { return this.lastChild; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  replaceChildren(...children) { this.childNodes = []; this.append(...children); }
  remove() {
    if (this.parent) {
      this.parent.childNodes = this.parent.childNodes.filter((child) => child !== this);
    }
  }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() {
    const scroll = this.parent?.scrollContainer;
    return {
      top: this.rectTop ?? (scroll
        ? (scroll.rectTop ?? 0) + (this.contentTop ?? scroll.scrollHeight) - scroll.scrollTop
        : 0),
    };
  }
  scrollTo(options) {
    this.scrollTop = Math.max(0, Math.min(options.top, this.scrollHeight - this.clientHeight));
    this.scrollCalls += 1;
    this.dispatchEvent(new Event("scroll"));
  }
  pause() {}
  load() {}
  disconnect() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatchEvent(new Event("close")); }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click")); }
}

function fixture(settings = {}) {
  const elements = new Map();
  const element = (id) => {
    assert(!["record", "stt-toggle", "tts-toggle"].includes(id), `Removed control used: ${id}`);
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const document = new EventTarget();
  document.hidden = false;
  document.body = new Element("body");
  document.documentElement = new Element("html");
  if (settings.basePath) document.documentElement.setAttribute("data-base-path", settings.basePath);
  document.documentElement.style.setProperty = function (key, value) { this[key] = value; };
  document.getElementById = element;
  element("messages").scrollContainer = element("conversation-scroll");
  document.createElement = () => new Element();
  document.createElementNS = () => new Element();
  document.createTextNode = (text) => {
    const node = new Element(); node.textContent = text; return node;
  };
  const topics = [new Element("topic")];
  const navLinks = ["#conversation", "#topics", "#screen-settings"].map((href, index) => {
    const link = element(`nav-${index}`);
    link.setAttribute("href", href);
    link.classList.add("nav-item");
    return link;
  });
  navLinks[2].classList.add("settings-trigger");
  const settingsTriggers = [navLinks[2], element("open-settings-top")];
  topics[0].dataset.prompt = "잠을 잘 자려면 어떻게 하나요?";
  document.querySelectorAll = (selector) => {
    if (selector === "[data-prompt]") return topics;
    if (selector === ".settings-trigger") return settingsTriggers;
    if (selector === ".section-nav .nav-item") return navLinks;
    const descendants = (node) => [node, ...node.childNodes.flatMap(descendants)];
    if (selector === ".speak-action") {
      return descendants(element("messages")).filter((node) => node.className === "speak-action");
    }
    return [];
  };
  document.querySelector = (selector) => selector === ".topbar-settings"
    ? settingsTriggers[1]
    : selector === ".section-nav .settings-trigger" ? settingsTriggers[0] : null;
  let focusCalls = 0;
  Element.prototype.focus = function () { focusCalls += 1; document.activeElement = this; };
  Element.prototype.blur = function () {
    if (document.activeElement === this) document.activeElement = null;
  };
  element("status").append(new Element());
  element("audio-panel").hidden = true;
  const loading = new Element();
  loading.className = "loading";
  element("loading-template").content = loading;
  const permissions = [];
  const streams = [];
  const recorders = [];
  const contexts = [];
  const outputs = [];
  const requests = [];
  const handlers = new Map();
  const timers = new Map();
  const lifecycle = new EventTarget();
  const createdURLs = [];
  const resizeObservers = [];
  const visualViewport = new EventTarget();
  visualViewport.height = 700;
  visualViewport.offsetTop = 0;
  let now = 0;
  let timerID = 0;
  let rms = 0;
  let plays = 0;
  let decode = async () => ({ duration: 1 });
  class Recorder extends EventTarget {
    static isTypeSupported() { return !settings.unsupportedFormat; }
    constructor(stream) {
      super();
      this.stream = stream;
      this.state = "inactive";
      recorders.push(this);
    }
    start(timeslice) { this.state = "recording"; this.timeslice = timeslice; }
    emit(blob) {
      const event = new Event("dataavailable");
      event.data = blob;
      this.dispatchEvent(event);
    }
    stop() {
      if (this.state === "inactive") return;
      this.state = "inactive";
      queueMicrotask(() => {
        this.emit(new Blob(["recorded audio"]));
        this.dispatchEvent(new Event("stop"));
      });
    }
  }
  class AudioContext {
    constructor() {
      this.state = "suspended";
      this.destination = {};
      this.resumes = 0;
      contexts.push(this);
    }
    async resume() { this.resumes += 1; this.state = "running"; }
    async close() { this.state = "closed"; }
    createAnalyser() {
      return { fftSize: 2048, disconnect() {}, getFloatTimeDomainData(data) { data.fill(rms); } };
    }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    decodeAudioData(data) { return decode(data); }
    createBufferSource() {
      const output = {
        starts: 0, stops: 0, onended: null,
        connect() {}, disconnect() {},
        start() { this.starts += 1; },
        stop() { this.stops += 1; },
        end() { this.onended?.(); },
      };
      outputs.push(output);
      return output;
    }
  }
  element("audio").play = async () => { plays += 1; };
  const answer = (text = "충분히 쉬어 주세요.", mode = "general") => ({
    answer: text, mode, sources: [], notice: "참고 정보예요.", health_topics: [],
  });
  const json = (data) => ({ ok: true, json: async () => data });
  const audio = () => ({ ok: true, blob: async () => new Blob(["answer audio"]) });
  const context = vm.createContext({
    console, Blob, FormData, Event, AbortController, Float32Array,
    visualViewport,
    requestAnimationFrame: (callback) => setImmediate(callback),
    ResizeObserver: class {
      constructor(callback) {
        this.callback = callback; this.targets = []; resizeObservers.push(this);
      }
      observe(target) { this.targets.push(target); }
    },
    MediaRecorder: settings.noRecorder ? undefined : Recorder,
    AudioContext: settings.noAudio ? undefined : AudioContext,
    document,
    navigator: {
      userAgent: "frontend-test", platform: "Linux", maxTouchPoints: 0,
      mediaDevices: settings.noMicrophone ? undefined : {
        getUserMedia() {
          assert(!streams.some((stream) => !stream.track.stops), "Microphones must never overlap");
          const permission = deferred();
          permissions.push(permission);
          return permission.promise;
        },
      },
    },
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() {} },
    matchMedia: () => ({ matches: true }),
    Date: class extends Date { static now() { return now; } },
    addEventListener: lifecycle.addEventListener.bind(lifecycle),
    setTimeout(callback, delay) {
      const id = ++timerID;
      timers.set(id, { callback, at: now + delay, delay });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    URL: class extends URL {
      static createObjectURL(blob) { createdURLs.push(blob); return "blob:fixture"; }
      static revokeObjectURL() {}
    },
    fetch: async (path, options) => {
      const requestPath = path;
      if (settings.basePath) {
        assert(path.startsWith(settings.basePath + "/"), "Never call a different profile's API");
        path = path.slice(settings.basePath.length);
      }
      if (path === "/api/config") return json({
        voice_enabled: settings.voiceEnabled ?? true,
        search_enabled: settings.searchEnabled ?? true, health_info_enabled: false,
        service_variant: settings.variant ?? "azure",
        request_timeout_ms: settings.requestTimeout ?? 120000,
      });
      requests.push({ path: requestPath, options });
      if (path.startsWith("/api/speech/") || path === "/api/chat") {
        assert(
          !streams.some((stream) => !stream.track.stops), "Stop microphone before server requests",
        );
      }
      const queue = handlers.get(path);
      if (queue?.length) return queue.shift()(options);
      if (path === "/api/speech/transcribe") return json({ text: "잠이 잘 오지 않아요." });
      if (path === "/api/chat") return json(answer());
      if (path === "/api/speech/synthesize") return audio();
      assert.fail(`Unexpected API request: ${path}`);
    },
  });
  vm.runInContext(fs.readFileSync("silvercare/static/app.js", "utf8"), context);
  const app = {
    element, document, topics, permissions, streams, recorders, contexts, outputs, requests,
    timers, lifecycle, createdURLs, answer, json, audio, flush, settingsTriggers, visualViewport,
    navLinks, location: context.location,
    state: vm.runInContext("state", context),
    voice: vm.runInContext("voice", context),
    send: vm.runInContext("sendMessage", context),
    speak: vm.runInContext("speakText", context),
    stopAudio: vm.runInContext("stopAudio", context),
    pause: vm.runInContext("pauseVoice", context),
    focusCalls: () => focusCalls,
    resize() { resizeObservers.forEach((observer) => observer.callback()); },
    plays: () => plays,
    setRMS(value) { rms = value; },
    setDecode(handler) { decode = handler; },
    respond(path, handler) {
      if (!handlers.has(path)) handlers.set(path, []);
      handlers.get(path).push(handler);
    },
    async tick(ms) {
      const until = now + ms;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= until)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
        await flush();
      }
      now = until;
      await flush();
    },
    async grant(index = permissions.length - 1) {
      const track = { stops: 0, stop() { this.stops += 1; } };
      const stream = { track, getTracks: () => [track] };
      streams.push(stream);
      permissions[index].resolve(stream);
      await flush();
      return stream;
    },
    async start() {
      element("mode-voice").click();
      await flush();
      await app.grant();
      assert.equal(app.voice.phase, "listening");
    },
    async finish() { element("voice-finish").click(); await flush(); },
    count(path) { return requests.filter((request) => request.path === path).length; },
    cancel(kind) {
      if (kind === "pause") element("voice-pause").click();
      if (kind === "switch") element("mode-text").click();
      if (kind === "reset") element("new-chat").click();
      if (kind === "pagehide") lifecycle.dispatchEvent(new Event("pagehide"));
      if (kind === "hidden") {
        document.hidden = true;
        document.dispatchEvent(new Event("visibilitychange"));
      }
    },
  };
  return app;
}

async function run(test, settings) {
  const app = fixture(settings);
  try { await flush(); await test(app); }
  finally { app.pause(); app.stopAudio(); }
}
"""


@unittest.skipUnless(NODE, "Node.js is required for frontend regression tests")
class FrontendVoiceTests(unittest.TestCase):
    def run_javascript(self, scenario, settings="{}"):
        script = (
            HARNESS
            + "\nrun(async (app) => {\n"
            + scenario
            + "\n}, "
            + settings
            + r"""
).then(
  () => console.log("Frontend scenario passed"),
  (error) => { console.error(error); process.exitCode = 1; },
);
"""
        )
        result = subprocess.run(
            [NODE, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Frontend scenario passed", result.stdout, "Scenario did not finish")

    def test_voice_is_opt_in_and_text_send_has_no_automatic_audio(self):
        self.run_javascript(r"""
assert.equal(app.permissions.length, 0);
assert.equal(app.contexts.length, 0);
assert.equal(app.requests.length, 0);
app.element("message").value = "안녕하세요";
app.element("chat-form").dispatchEvent(new Event("submit", { cancelable: true }));
await app.flush();
assert.equal(app.state.history.length, 2);
assert.equal(app.state.history[0].content, "안녕하세요");
assert.equal(app.count("/api/speech/synthesize"), 0);
assert.equal(app.focusCalls(), 1);
await app.speak("이 답변을 읽어요.");
assert.equal(app.plays(), 1);
assert.equal(app.createdURLs.length, 1);
assert.equal(app.permissions.length, 0);
""")

    def test_comparison_api_and_images_stay_within_their_profile_prefix(self):
        self.run_javascript(
            r"""
await app.send("서비스 구분 확인입니다.");
assert.equal(app.requests[0].path, "/local-rag/api/chat");
const reply = app.element("messages").lastElementChild;
assert.equal(reply.childNodes[0].src, "/local-rag/static/mark.svg");
assert.equal(app.element("status").lastChild.textContent, "상담 준비 완료");
assert.equal(app.permissions.length, 0);
""",
            '{basePath: "/local-rag", variant: "local-rag", requestTimeout: 600000}',
        )

    def test_qwen_azure_requests_keep_their_prefix_without_a_profile_label(self):
        self.run_javascript(
            r"""
await app.send("서비스 구분 확인입니다.");
assert.equal(app.requests[0].path, "/qwen-azure-rag/api/chat");
assert.equal(app.element("status").lastChild.textContent, "상담 준비 완료");
""",
            '{basePath: "/qwen-azure-rag", variant: "qwen-azure-rag", requestTimeout: 600000}',
        )

    def test_model_only_uses_the_same_ready_header_without_exposing_search_mode(self):
        self.run_javascript(
            r"""
assert.equal(app.element("status").lastChild.textContent, "상담 준비 완료");
await app.send("공통 화면 확인입니다.");
assert.equal(app.requests[0].path, "/local-only/api/chat");
""",
            '{basePath: "/local-only", variant: "local-only", searchEnabled: false}',
        )

    def test_complete_voice_turn_uses_shared_memory_and_waits_for_playback_end(self):
        self.run_javascript(r"""
await app.send("글로 먼저 물어봐요.");
await app.start();
const focusBefore = app.focusCalls();
await app.finish();
assert.deepEqual(app.requests.map((request) => request.path), [
  "/api/chat", "/api/speech/transcribe", "/api/chat", "/api/speech/synthesize",
]);
assert.equal(app.voice.phase, "speaking");
assert.equal(app.permissions.length, 1, "Do not listen to the assistant's own voice");
assert.equal(app.streams[0].track.stops, 1);
assert.equal(app.state.history.length, 4);
const chat = app.requests.filter((request) => request.path === "/api/chat")[1];
assert.equal(JSON.parse(chat.options.body).history.length, 2);
assert.equal(JSON.parse(chat.options.body).message, "잠이 잘 오지 않아요.");
assert.equal(app.element("message").value, "");
assert.equal(app.element("voice-transcript").textContent, "잠이 잘 오지 않아요.");
assert.equal(app.element("voice-caption").textContent, app.state.history[3].content);
assert.equal(app.focusCalls(), focusBefore);
assert.equal(app.contexts.length, 1);
assert.equal(app.contexts[0].resumes, 1);
assert.equal(app.outputs[0].starts, 1);
assert.equal(app.plays(), 0, "Automatic voice uses unlocked Web Audio, not HTMLAudio autoplay");
app.outputs[0].end();
await app.flush();
assert.equal(app.permissions.length, 2);
await app.grant();
assert.equal(app.voice.phase, "listening");
assert.equal(app.contexts.length, 1, "Reuse the user-unlocked context for later turns");
await app.finish();
assert.equal(app.state.history.length, 6);
const thirdChat = app.requests.filter(r => r.path === "/api/chat")[2];
assert.equal(JSON.parse(thirdChat.options.body).history.length, 4);
""")

    def test_reading_older_messages_does_not_jump_on_reply_and_new_answer_restores_latest(self):
        self.run_javascript(r"""
await app.send("첫 질문이에요.");
const delayed = deferred();
app.respond("/api/chat", () => delayed.promise);
const sending = app.send("둘째 질문이에요.");
await app.flush();
const scroll = app.element("conversation-scroll");
scroll.scrollTop = 40;
scroll.dispatchEvent(new Event("scroll"));
const scrollCalls = scroll.scrollCalls, focusCalls = app.focusCalls();
delayed.resolve(app.json(app.answer("새 답변입니다.")));
await sending;
assert.equal(scroll.scrollTop, 40, "Reading position must not change on delayed reply");
assert.equal(scroll.scrollCalls, scrollCalls);
assert.equal(app.focusCalls(), focusCalls, "Do not steal focus while reading");
assert.equal(app.element("new-answer").hidden, false);
app.element("new-answer").click();
assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight);
assert.equal(app.element("new-answer").hidden, true);
assert.equal(app.state.history.length, 4);
app.element("new-chat").click();
assert.equal(scroll.scrollTop, 0);
assert.equal(app.element("new-answer").hidden, true);
assert.equal(app.state.history.length, 0);
""")

    def test_mobile_viewport_resize_keeps_latest_without_mistaking_layout_for_browsing(self):
        self.run_javascript(r"""
const scroll = app.element("conversation-scroll");
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 0, "Opening the welcome screen must not jump to the bottom");
await app.send("모바일 대화입니다.");
await app.flush();
scroll.clientHeight = 220;
scroll.dispatchEvent(new Event("scroll"));
app.visualViewport.height = 400;
app.visualViewport.dispatchEvent(new Event("resize"));
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 780, "Follow the bottom after the keyboard reduces the viewport");
assert.equal(app.element("new-answer").hidden, true);
scroll.scrollHeight = 1500;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 1280, "Follow asynchronous content growth");
scroll.clientHeight = 500;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 1000, "Keep latest after closing the keyboard");
""")

    def test_reader_position_and_topics_survive_resizes_and_pending_follow_frames(self):
        self.run_javascript(r"""
await app.send("기존 대화입니다.");
await app.flush();
const scroll = app.element("conversation-scroll");
scroll.scrollTop = 100;
scroll.dispatchEvent(new Event("scroll"));
scroll.clientHeight = 200;
app.visualViewport.height = 350;
app.visualViewport.dispatchEvent(new Event("resize"));
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 100, "Keyboard changes must not pull a reader to the bottom");
app.element("new-answer").click();
scroll.scrollTop = 90;
scroll.dispatchEvent(new Event("scroll"));
await app.flush();
assert.equal(scroll.scrollTop, 90, "User scrolling must cancel a queued follow");
app.navLinks[1].click();
const topicPosition = scroll.scrollTop;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, topicPosition, "Topics navigation must not snap back to the chat");
app.element("new-chat").click();
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 0, "Reset must preserve the welcome position");
""")

    def test_delayed_mobile_reply_follows_after_viewport_and_input_height_changes(self):
        self.run_javascript(r"""
const delayed = deferred();
app.respond("/api/chat", () => delayed.promise);
app.navLinks[1].click();
const sending = app.send("주제에서 질문합니다.");
await app.flush();
const scroll = app.element("conversation-scroll");
assert.equal(app.navLinks[0].getAttribute("aria-current"), "location");
scroll.clientHeight = 180;
scroll.dispatchEvent(new Event("scroll"));
app.element("message").dispatchEvent(new Event("input"));
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 820);
scroll.scrollHeight = 1800;
delayed.resolve(app.json(app.answer("길어진 모바일 답변입니다.")));
await sending;
await app.flush();
assert.equal(scroll.scrollTop, 1620);
assert.equal(app.element("new-answer").hidden, true);
""")

    def test_touch_reading_during_keyboard_resize_overrides_the_pending_follow(self):
        self.run_javascript(r"""
await app.send("모바일 대화입니다.");
await app.flush();
const scroll = app.element("conversation-scroll");
scroll.clientHeight = 220;
app.resize();
scroll.dispatchEvent(new Event("touchmove"));
scroll.scrollTop = 90;
scroll.dispatchEvent(new Event("scroll"));
await app.flush();
assert.equal(scroll.scrollTop, 90, "An actual reading gesture wins over concurrent layout changes");
""")

    def test_long_reply_opens_at_its_start_and_stays_there_when_footer_and_viewport_change(self):
        self.run_javascript(r"""
const scroll = app.element("conversation-scroll");
scroll.scrollHeight = 3000;
app.element("messages").childContentTop = 800;
await app.send("여러 항목을 자세히 설명해 주세요.");
await app.flush();
assert.equal(scroll.scrollTop, 784, "Open the answer start, not its citations at the bottom");
assert.equal(app.element("messages").lastElementChild.getBoundingClientRect().top, 16);
assert.equal(app.element("new-answer").hidden, true);
scroll.scrollHeight = 4500;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 784, "Additional sources must not move the answer out of view");
scroll.clientHeight = 180;
app.visualViewport.height = 350;
app.visualViewport.dispatchEvent(new Event("resize"));
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 784, "Keyboard changes preserve the answer start");
scroll.clientHeight = 650;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 784);
assert.equal(app.element("messages").lastElementChild.getBoundingClientRect().top, 16);
""")

    def test_reading_down_a_long_answer_is_not_pulled_back_and_new_answer_opens_its_start(self):
        self.run_javascript(r"""
const scroll = app.element("conversation-scroll");
scroll.scrollHeight = 3000;
app.element("messages").childContentTop = 800;
await app.send("긴 답변을 읽겠습니다.");
await app.flush();
scroll.dispatchEvent(new Event("touchmove"));
scroll.scrollTop = 1000;
scroll.dispatchEvent(new Event("scroll"));
scroll.clientHeight = 250;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 1000, "Manual reading wins over the start anchor");
const delayed = deferred();
app.respond("/api/chat", () => delayed.promise);
app.element("messages").childContentTop = 2000;
const sending = app.send("다음 답변도 자세히 알려주세요.");
await app.flush();
scroll.dispatchEvent(new Event("wheel"));
scroll.scrollTop = 1100;
scroll.dispatchEvent(new Event("scroll"));
delayed.resolve(app.json(app.answer("새로 온 긴 답변입니다.")));
await sending;
await app.flush();
assert.equal(scroll.scrollTop, 1100);
assert.equal(app.element("new-answer").hidden, false);
app.element("new-answer").click();
await app.flush();
assert.equal(scroll.scrollTop, 1984, "The new-answer action opens the reply, not the page bottom");
assert.equal(app.element("new-answer").hidden, true);
assert.equal(app.state.history.length, 4);
""")

    def test_short_answer_fits_and_opening_official_material_does_not_force_a_jump(self):
        self.run_javascript(r"""
const scroll = app.element("conversation-scroll");
scroll.scrollHeight = 1000;
app.element("messages").childContentTop = 800;
await app.send("전화번호만 알려주세요.");
await app.flush();
assert.equal(scroll.scrollTop, 500, "Clamp short replies to the available content");
app.element("messages").dispatchEvent(new Event("toggle"));
scroll.scrollHeight = 2500;
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 500, "Opening original material must preserve the reading position");
app.element("new-chat").click();
app.resize();
await app.flush();
assert.equal(scroll.scrollTop, 0, "Reset clears a pending answer anchor");
""")

    def test_settings_dialog_pauses_voice_retains_history_and_does_not_restart_on_close(self):
        self.run_javascript(r"""
await app.send("기억할 질문입니다.");
await app.start();
app.settingsTriggers[0].click();
assert.equal(app.element("screen-settings").open, true);
assert.equal(app.voice.active, false);
assert.equal(app.streams[0].track.stops, 1);
assert.equal(app.state.history.length, 2);
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-pause");
app.element("font-large").click();
assert.equal(app.document.body.classList.contains("large-text"), true);
app.element("close-settings").click();
assert.equal(app.element("screen-settings").open, false);
assert.equal(app.document.activeElement, app.settingsTriggers[0]);
assert.equal(app.permissions.length, 1, "Closing settings must not resume mic");
assert.equal(app.state.history.length, 2);
app.settingsTriggers[1].click();
assert.equal(app.element("screen-settings").open, true);
const escape = new Event("keydown", { cancelable: true });
escape.key = "Escape";
app.element("screen-settings").dispatchEvent(escape);
assert.equal(app.element("screen-settings").open, false);
assert.equal(escape.defaultPrevented, true);
assert.equal(app.document.activeElement, app.settingsTriggers[1]);
""")

    def test_topics_navigation_aligns_the_section_opens_library_and_preserves_conversation(self):
        self.run_javascript(r"""
await app.send("이어갈 대화입니다.");
const scroll = app.element("conversation-scroll");
scroll.rectTop = 64;
scroll.scrollTop = 100;
app.element("topics").rectTop = 200;
app.element("health-library").hidden = false;
app.element("health-library").open = false;
const requests = app.requests.length;
app.navLinks[1].click();
assert.equal(scroll.scrollTop, 216, "Align topics inside the conversation scroller");
assert.equal(app.element("health-library").open, true);
assert.equal(app.document.activeElement, app.element("topics"));
assert.equal(app.element("current-section-title").textContent, "함께 알아보기");
assert.equal(app.element("topics-title").textContent, "함께 알아보기");
assert.equal(app.navLinks[1].getAttribute("aria-current"), "location");
assert.equal(app.requests.length, requests, "Navigation must not send a sample question");
assert.equal(app.state.history.length, 2);
app.navLinks[0].click();
assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight);
assert.equal(app.element("current-section-title").textContent, "온담과 대화하기");
assert.equal(app.state.history.length, 2);
app.element("health-library").open = false;
app.location.hash = "#topics";
app.lifecycle.dispatchEvent(new Event("hashchange"));
assert.equal(app.element("health-library").open, true);
assert.equal(app.navLinks[1].getAttribute("aria-current"), "location");
assert.equal(app.requests.length, requests);
""")

    def test_settings_cancel_pending_permission_before_its_late_resolution(self):
        self.run_javascript(r"""
app.element("mode-voice").click();
await app.flush();
assert.equal(app.permissions.length, 1);
app.settingsTriggers[0].click();
await app.grant();
assert.equal(app.voice.active, false);
assert.equal(app.streams[0].track.stops, 1);
assert.equal(app.element("screen-settings").open, true);
assert.equal(app.voice.recorder, null);
app.element("close-settings").click();
assert.equal(app.permissions.length, 1);
""")

    def test_visual_viewport_height_and_offset_update_without_new_audio_requests(self):
        self.run_javascript(r"""
assert.equal(app.document.documentElement.style["--app-height"], "700px");
app.visualViewport.height = 350;
app.visualViewport.offsetTop = 60;
app.visualViewport.dispatchEvent(new Event("resize"));
assert.equal(app.document.documentElement.style["--app-height"], "350px");
assert.equal(app.document.documentElement.style["--app-top"], "60px");
app.visualViewport.offsetTop = 0;
app.visualViewport.dispatchEvent(new Event("scroll"));
assert.equal(app.document.documentElement.style["--app-top"], "0px");
assert.equal(app.document.documentElement.getAttribute("data-viewport-size"), "compact");
app.visualViewport.height = 250;
app.visualViewport.dispatchEvent(new Event("resize"));
assert.equal(app.document.documentElement.getAttribute("data-viewport-size"), "tiny");
app.visualViewport.height = 800;
app.visualViewport.dispatchEvent(new Event("resize"));
assert.equal(app.document.documentElement.getAttribute("data-viewport-size"), "regular");
assert.equal(app.permissions.length, 0);
assert.equal(app.requests.length, 0);
""")

    def test_microphone_visual_tracks_actual_input_not_a_decorative_sound_animation(self):
        self.run_javascript(r"""
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "paused");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-pause");
assert.equal(app.element("voice-finish").hidden, true);
assert.equal(app.element("voice-pause").hidden, true);
app.element("mode-voice").click();
await app.flush();
assert.equal(app.voice.phase, "permission");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-wait");
assert.equal(app.element("voice-input-feedback").hidden, true);
assert.equal(app.element("voice-processing").hidden, false);
await app.grant();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "listening");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-mic");
assert.equal(app.element("voice-state-label").textContent, "듣고 있어요");
assert.equal(app.element("voice-input-feedback").hidden, false);
assert.equal(app.element("voice-processing").hidden, true);
assert.equal(app.element("voice-start").hidden, true);
assert.equal(app.element("voice-finish").hidden, false);
assert.equal(app.element("voice-pause").hidden, false);
await app.tick(100);
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
app.setRMS(0.03);
await app.tick(100);
const low = Number(app.element("voice-input-meter").getAttribute("data-level"));
assert(low > 0 && low < 5);
assert.equal(app.element("voice-input-label").textContent, "소리가 들어오고 있어요");
assert.doesNotMatch(app.element("voice-input-label").textContent, /인식 완료/);
app.setRMS(0.2);
await app.tick(100);
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "5");
app.setRMS(0);
await app.tick(100);
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
assert.equal(app.element("voice-input-label").textContent, "편하게 말씀해 주세요");
assert.equal(app.element("voice-state-label").textContent, "듣고 있어요");
app.element("voice-pause").click();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "paused");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-pause");
assert.equal(app.element("voice-input-feedback").hidden, true);
assert.equal(app.element("voice-processing").hidden, true);
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
assert.equal(app.element("voice-start").hidden, false);
assert.equal(app.element("voice-start").textContent, "대화 시작");
assert.equal(app.element("voice-pause").hidden, true);
assert.equal(app.streams[0].track.stops, 1);
""")

    def test_processing_and_playback_have_distinct_visuals_and_contextual_actions(self):
        self.run_javascript(r"""
const stt = deferred(), chat = deferred(), tts = deferred();
app.respond("/api/speech/transcribe", () => stt.promise);
app.respond("/api/chat", () => chat.promise);
app.respond("/api/speech/synthesize", () => tts.promise);
await app.start();
app.setRMS(0.08);
await app.tick(300);
await app.finish();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "transcribing");
assert.equal(app.element("voice-state-label").textContent, "말씀을 확인하고 있어요");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-wait");
assert.equal(app.element("voice-processing").hidden, false);
assert.equal(app.element("voice-input-feedback").hidden, true);
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
assert.equal(app.element("voice-start").hidden, true);
assert.equal(app.element("voice-finish").hidden, true);
assert.equal(app.element("voice-pause").hidden, false);
assert.equal(app.streams[0].track.stops, 1);
stt.resolve(app.json({ text: "잠이 잘 오지 않아요." }));
await app.flush();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "thinking");
assert.equal(app.element("voice-state-label").textContent, "답변을 생각하고 있어요");
chat.resolve(app.json(app.answer()));
await app.flush();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "preparing");
assert.equal(app.element("voice-state-label").textContent, "답변을 준비하고 있어요");
assert.equal(app.element("voice-start").textContent, "답변 멈추고 말하기");
assert.equal(app.element("voice-start").hidden, false);
tts.resolve(app.audio());
await app.flush();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "speaking");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-speaker");
assert.equal(app.element("voice-state-label").textContent, "온담이 이야기하고 있어요");
assert.equal(app.element("voice-processing").hidden, true);
assert.equal(app.element("voice-input-feedback").hidden, true);
assert.equal(app.element("voice-start").disabled, false);
assert.equal(app.permissions.length, 1, "Speaker state must not record");
app.outputs[0].end();
await app.flush();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "permission");
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
await app.grant();
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "listening");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-mic");
assert.equal(app.element("voice-start").hidden, true);
""")

    def test_input_visuals_clear_on_every_voice_cancellation_and_no_timers_restart_them(self):
        for kind in ["pause", "switch", "reset", "pagehide", "hidden"]:
            with self.subTest(kind=kind):
                self.run_javascript(
                    r"""
await app.start();
app.setRMS(0.1);
await app.tick(300);
assert(Number(app.element("voice-input-meter").getAttribute("data-level")) > 0);
app.cancel("""
                    + repr(kind)
                    + r""");
await app.tick(500);
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "paused");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-pause");
assert.equal(app.element("voice-processing").hidden, true);
assert.equal(app.element("voice-input-feedback").hidden, true);
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
assert.equal(app.streams[0].track.stops, 1);
assert.equal(app.permissions.length, 1);
assert.equal(app.timers.size, 0);
"""
                )

    def test_repeated_record_and_finish_clicks_do_not_duplicate_requests(self):
        self.run_javascript(r"""
await app.start();
app.element("mode-voice").click();
app.element("voice-start").click();
assert.equal(app.permissions.length, 1);
app.element("voice-finish").click();
app.element("voice-finish").click();
await app.flush();
assert.equal(app.count("/api/speech/transcribe"), 1);
assert.equal(app.count("/api/chat"), 1);
assert.equal(app.outputs.length, 1);
""")

    def test_history_stays_at_twelve_messages_and_renders_factual_sources(self):
        self.run_javascript(r"""
const result = app.answer("건강 정보 [1]입니다.");
result.sources = [{
  id: 1, title: "공식 자료", url: "https://health.kdca.go.kr/healthinfo",
  excerpt: "출처 내용", evidence_kind: "dataset_knowledge", source_display: "질병관리청",
  record_id: "record-1", source_year: "2025", updated_at: "2026-11-11",
}];
app.respond("/api/chat", async () => app.json(result));
await app.send("첫 질문");
const find = (node, name) => node.className === name ? node
  : node.childNodes.map(child => find(child, name)).find(Boolean);
const source = find(app.element("messages"), "source-link");
assert.equal(source.href, result.sources[0].url);
assert.match(source.title, /record-1/);
for (let i = 0; i < 7; i++) await app.send(`질문 ${i}`);
assert.equal(app.state.history.length, 12);
assert.equal(app.state.history[0].content, "질문 1");
assert.equal(JSON.parse(app.requests.at(-1).options.body).history.length, 12);
""")

    def test_who_license_moves_to_info_without_repeating_in_the_conversation(self):
        self.run_javascript(r"""
const result = app.answer("기능 저하에 관한 안내입니다. [1]");
result.sources = [{
  id: 1, title: "WHO ICOPE", url: "https://www.who.int/publications/i/item/9789240103726",
  excerpt: "원문 발췌", updated_at: "2025-09-22",
  reuse_notice: "WHO 원문, 비상업적 이용. 원문과 온담의 번역·가공을 구분합니다.",
  license_url: "https://creativecommons.org/licenses/by-nc-sa/4.0/",
}];
result.sources.push({ ...result.sources[0], id: 2, title: "WHO ICOPE 다른 쪽" });
app.respond("/api/chat", async () => app.json(result));
await app.send("기능 저하가 궁금해요.");
const find = (node, name) => node.className === name ? node
  : node.childNodes.map(child => find(child, name)).find(Boolean);
const notice = app.element("source-reuse-notices").childNodes[0];
assert.match(notice.textContent, /비상업적/);
assert.match(notice.textContent, /번역·가공/);
assert.equal(notice.childNodes[0].href, result.sources[0].license_url);
const sourceContainer = find(app.element("messages"), "sources");
assert.equal(sourceContainer.childNodes.length, 2);
assert.equal(sourceContainer.childNodes[0].className, "source-link");
assert.equal(sourceContainer.childNodes[1].className, "source-link");
assert.equal(sourceContainer.childNodes[0].href, result.sources[0].url);
assert.equal(find(app.element("messages"), "source-reuse-notice"), undefined);
assert.equal(app.element("source-reuse-info").hidden, false);
app.element("open-info").click();
assert.equal(app.element("info-dialog").open, true);
app.element("close-info").click();
await app.send("다른 질문입니다.");
assert.equal(app.element("source-reuse-notices").childNodes.length, 1);
""")

    def test_medical_reply_shows_one_combined_notice_and_keeps_official_cards(self):
        self.run_javascript(r"""
const result = app.answer("건강에 관한 안내입니다.");
result.notice = "기존 일반 AI 안내";
result.medical_caution = "기존 상세 의료 주의";
result.display_notice = "AI 답변에는 오류가 있을 수 있어요. 복약 변경은 의료진과 상의해 주세요.";
result.health_topics = [{ id: "6752", title: "건강노화",
  url: "https://health.kdca.go.kr/healthinfo/view.do?cntnts_sn=6752" }];
app.respond("/api/chat", async () => app.json(result));
await app.send("건강노화가 궁금해요.");
const all = (node) => [node, ...node.childNodes.flatMap(all)];
const nodes = all(app.element("messages"));
const notices = nodes.filter(
  node => ["medical-caution", "message-notice"].includes(node.className)
);
assert.equal(notices.length, 1);
assert.equal(notices[0].className, "medical-caution");
assert.equal(notices[0].childNodes[1].textContent, result.display_notice);
assert.equal(nodes.filter(node => node.className === "official-health-card").length, 1);
assert(!nodes.some(node => node.textContent === result.notice));
const redundantHint = "함께 확인할 공식 자료예요. 진단 결과나 개인별 권고는 아니에요.";
assert(!nodes.some(node => node.textContent === redundantHint));
assert.equal(app.count("/api/health-info/6752"), 0,
  "Opening the answer must not fetch original text");
""")

    def test_legacy_medical_reply_keeps_custom_caution_without_a_second_generic_notice(self):
        self.run_javascript(r"""
const result = app.answer("건강 안내입니다.");
result.notice = "기존 일반 안내";
result.medical_caution = "보존해야 하는 별도 의료 주의입니다.";
app.respond("/api/chat", async () => app.json(result));
await app.send("질문입니다.");
const all = (node) => [node, ...node.childNodes.flatMap(all)];
const notices = all(app.element("messages")).filter(
  node => ["medical-caution", "message-notice"].includes(node.className)
);
assert.equal(notices.length, 1);
assert.equal(notices[0].childNodes[1].textContent, result.medical_caution);
""")

    def test_nonmedical_reply_keeps_one_general_notice(self):
        self.run_javascript(r"""
const result = app.answer("일상 대화입니다.");
result.display_notice = "AI 답변에는 오류가 있을 수 있어요.";
app.respond("/api/chat", async () => app.json(result));
await app.send("오늘은 기분이 좋아요.");
const all = (node) => [node, ...node.childNodes.flatMap(all)];
const notices = all(app.element("messages")).filter(
  node => ["medical-caution", "message-notice"].includes(node.className)
);
assert.equal(notices.length, 1);
assert.equal(notices[0].className, "message-notice");
assert.equal(notices[0].textContent, result.display_notice);
""")

    def test_silence_needs_minimum_speech_and_three_second_trailing_pause(self):
        self.run_javascript(r"""
await app.start();
app.setRMS(0.08);
await app.tick(100);
app.setRMS(0);
await app.tick(4000);
assert.equal(app.count("/api/speech/transcribe"), 0, "Ignore a brief click");
app.setRMS(0.08);
await app.tick(300);
app.setRMS(0);
await app.tick(2900);
assert.equal(app.count("/api/speech/transcribe"), 0);
await app.tick(100);
assert.equal(app.count("/api/speech/transcribe"), 1);
assert.equal(app.voice.phase, "speaking");
""")

    def test_no_input_pauses_at_thirty_seconds_and_releases_resources(self):
        self.run_javascript(r"""
await app.start();
await app.tick(29900);
assert.equal(app.voice.active, true);
await app.tick(100);
assert.equal(app.voice.active, false);
assert.match(app.element("voice-status").textContent, /30초/);
assert.equal(app.count("/api/speech/transcribe"), 0);
assert.equal(app.streams[0].track.stops, 1);
assert.equal(app.contexts[0].state, "closed");
assert.equal(app.timers.size, 0);
""")

    def test_two_minute_limit_processes_turn_and_is_not_session_limit(self):
        self.run_javascript(r"""
const stt = deferred();
app.respond("/api/speech/transcribe", () => stt.promise);
await app.start();
app.setRMS(0.08);
await app.tick(120000);
assert.equal(app.count("/api/speech/transcribe"), 1);
assert.equal(app.voice.phase, "transcribing");
assert.match(app.element("voice-caption").textContent, /2분/);
assert.equal(app.streams[0].track.stops, 1);
stt.resolve(app.json({ text: "오래 말씀드렸어요." }));
await app.flush();
app.outputs[0].end();
await app.flush();
await app.grant();
assert.equal(app.voice.phase, "listening");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-mic");
assert.equal(app.voice.active, true);
""")

    def test_audio_size_limit_does_not_send_over_four_mebibytes(self):
        self.run_javascript(r"""
await app.start();
app.recorders[0].emit(new Blob([new Uint8Array(4 * 1024 * 1024 + 1)]));
await app.flush();
assert.equal(app.voice.active, false);
assert.equal(app.count("/api/speech/transcribe"), 0);
assert.match(app.element("error").textContent, /4MB/);
assert.equal(app.streams[0].track.stops, 1);
""")

    def test_exact_audio_and_recognition_limits_are_accepted(self):
        self.run_javascript(r"""
app.respond("/api/speech/transcribe", async () => app.json({ text: "가".repeat(2000) }));
await app.start();
const finalChunkSize = new Blob(["recorded audio"]).size;
app.recorders[0].emit(new Blob([new Uint8Array(4 * 1024 * 1024 - finalChunkSize)]));
await app.finish();
const stt = app.requests.find(request => request.path === "/api/speech/transcribe");
assert.equal(stt.options.body.get("audio").size, 4 * 1024 * 1024);
assert.equal(app.state.history[0].content.length, 2000);
assert.equal(app.voice.phase, "speaking");
""")

    def test_pending_permission_cancellation_and_repeated_clicks_never_overlap(self):
        for cancellation in ["pause", "switch", "reset", "pagehide", "hidden"]:
            with self.subTest(cancellation=cancellation):
                self.run_javascript(
                    r"""
app.element("mode-voice").click();
await app.flush();
assert.equal(app.permissions.length, 1);
app.element("mode-voice").click();
app.element("voice-start").click();
assert.equal(app.permissions.length, 1);
app.cancel("""
                    + repr(cancellation)
                    + r""");
app.element("voice-start").click();
app.element("mode-voice").click();
assert.equal(app.permissions.length, 1, "Do not overlap a still-pending permission prompt");
assert.match(app.element("voice-status").textContent, /열려 있는 마이크 허용 창/);
const stream = await app.grant();
assert.equal(stream.track.stops, 1);
assert.equal(app.voice.stream, null);
assert.equal(app.voice.recorder, null);
assert.equal(app.voice.active, false);
assert.equal(app.timers.size, 0);
assert.equal(app.contexts[0].state, "closed");
assert.doesNotMatch(app.element("voice-status").textContent, /열려 있는 마이크 허용 창/);
"""
                )

    def test_pending_stt_and_chat_results_are_ignored_after_cancellation(self):
        for path in ["/api/speech/transcribe", "/api/chat"]:
            for cancellation in ["pause", "switch", "reset", "pagehide", "hidden"]:
                with self.subTest(path=path, cancellation=cancellation):
                    self.run_javascript(
                        r"""
const pending = deferred();
app.respond("""
                        + repr(path)
                        + r""", () => pending.promise);
await app.start();
await app.finish();
const request = app.requests.at(-1);
app.cancel("""
                        + repr(cancellation)
                        + r""");
assert.equal(request.options.signal.aborted, true);
pending.resolve(app.json("""
                        + (
                            "{ text: '늦게 인식한 말' }"
                            if path.endswith("transcribe")
                            else "app.answer()"
                        )
                        + r"""));
await app.flush();
assert.equal(app.state.history.length, 0);
assert.equal(app.outputs.length, 0);
assert.equal(app.voice.active, false);
assert.equal(app.state.busy, false);
assert.equal(app.element("message").disabled, false);
assert.equal(app.permissions.length, 1);
"""
                    )

    def test_cancellation_after_audio_blob_or_decode_does_not_play_or_restart(self):
        for stage in ["blob", "arrayBuffer", "decode"]:
            for cancellation in ["pause", "switch", "reset", "pagehide", "hidden"]:
                with self.subTest(stage=stage, cancellation=cancellation):
                    self.run_javascript(
                        r"""
const delayed = deferred();
const stage = """
                        + repr(stage)
                        + r""";
if (stage === "decode") app.setDecode(() => delayed.promise);
if (stage === "blob") {
  app.respond("/api/speech/synthesize", async () => ({ ok: true, blob: () => delayed.promise }));
}
if (stage === "arrayBuffer") {
  app.respond("/api/speech/synthesize", async () => ({
    ok: true, blob: async () => ({ arrayBuffer: () => delayed.promise }),
  }));
}
await app.start();
await app.finish();
assert.equal(app.state.history.length, 2, "Chat answer was already accepted");
const synthesis = app.requests.at(-1);
app.cancel("""
                        + repr(cancellation)
                        + r""");
delayed.resolve(stage === "blob" ? new Blob(["voice"]) : new ArrayBuffer(8));
await app.flush();
assert.equal(synthesis.options.signal.aborted, true);
assert.equal(app.outputs.length, 0);
assert.equal(app.plays(), 0);
assert.equal(app.permissions.length, 1);
assert.equal(app.state.history.length, """
                        + ("0" if cancellation == "reset" else "2")
                        + r""");
assert.equal(app.voice.active, false);
"""
                    )

    def test_speaking_start_interrupts_output_and_topics_do_not_echo(self):
        self.run_javascript(r"""
await app.start();
await app.finish();
const output = app.outputs[0];
const staleEnded = output.onended;
app.element("voice-start").click();
await app.flush();
assert.equal(output.stops, 1);
assert.equal(app.permissions.length, 2);
staleEnded();
assert.equal(app.permissions.length, 2, "Canceled output cannot restart a second microphone");
await app.grant();
app.topics[0].click();
await app.flush();
assert.equal(app.streams[1].track.stops, 1);
assert.equal(app.voice.phase, "speaking");
assert.equal(app.state.history[2].content, app.topics[0].dataset.prompt);
assert.equal(app.element("message").value, "");
assert.equal(app.focusCalls(), 0);
app.outputs[1].end();
await app.flush();
assert.equal(app.permissions.length, 3);
""")

    def test_stale_stt_cannot_overwrite_a_restarted_session(self):
        self.run_javascript(r"""
const stale = deferred();
app.respond("/api/speech/transcribe", () => stale.promise);
await app.start();
await app.finish();
app.element("voice-pause").click();
app.element("voice-start").click();
await app.flush();
await app.grant();
const currentContext = app.voice.context;
stale.resolve(app.json({ text: "취소한 예전 질문" }));
await app.flush();
assert.equal(app.voice.phase, "listening");
assert.equal(app.voice.context, currentContext);
assert.equal(app.state.history.length, 0);
assert.equal(app.count("/api/chat"), 0);
await app.finish();
assert.equal(app.state.history[0].content, "잠이 잘 오지 않아요.");
""")

    def test_manual_answer_playback_pauses_voice_and_cannot_echo(self):
        self.run_javascript(r"""
await app.start();
await app.speak("지난 답변을 다시 들려드려요.");
assert.equal(app.voice.active, false);
assert.equal(app.streams[0].track.stops, 1);
assert.equal(app.plays(), 1);
assert.equal(app.permissions.length, 1);
assert.equal(app.count("/api/speech/transcribe"), 0);
""")

    def test_voice_and_manual_playback_preserve_display_answer_and_use_shared_server_conversion(
        self,
    ):
        self.run_javascript(r"""
const raw = "**중요한 안내입니다.** 119에 도움을 요청하세요.[5]\n\n출처:\n[5] 합성 출처";
app.respond("/api/chat", async () => app.json(app.answer(raw)));
await app.start();
await app.finish();
const voiceRequest = app.requests.filter(r => r.path === "/api/speech/synthesize")[0];
assert.equal(JSON.parse(voiceRequest.options.body).text, raw,
  "Server must receive citation structure to distinguish references from answer body");
assert.equal(app.state.history[1].content, raw, "Screen/history answer must not be rewritten");
app.pause();
await app.speak(raw);
const manualRequest = app.requests.filter(r => r.path === "/api/speech/synthesize")[1];
assert.equal(JSON.parse(manualRequest.options.body).text, raw,
  "Manual listening must use the same server conversion as voice mode");
""")

    def test_topics_interrupt_current_speech_without_waiting_for_audio_end(self):
        self.run_javascript(r"""
await app.start();
await app.finish();
app.topics[0].click();
await app.flush();
assert.equal(app.outputs[0].stops, 1);
assert.equal(app.outputs.length, 2);
assert.equal(app.count("/api/chat"), 2);
assert.equal(app.permissions.length, 1);
""")

    def test_empty_invalid_and_overlong_recognition_pause_without_truncating(self):
        for recognized in ['""', '"   "', "null", "42", '"가".repeat(2001)']:
            with self.subTest(recognized=recognized):
                self.run_javascript(
                    r"""
const recognized = """
                    + recognized
                    + r""";
app.respond("/api/speech/transcribe", async () => app.json({ text: recognized }));
await app.start();
await app.finish();
assert.equal(app.count("/api/chat"), 0);
assert.equal(app.voice.active, false);
assert.equal(app.voice.context, null);
assert.equal(app.element("error").hidden, false);
if (typeof recognized === "string" && recognized.length > 2000) {
  assert.equal(app.element("voice-transcript").textContent, recognized);
  app.element("mode-text").click();
  assert.equal(app.element("message").value, recognized, "Never silently truncate recognition");
  assert.equal(app.element("chat-form").hidden, false);
  app.element("message").value = "고친 내용";
  await app.send(app.element("message").value);
  assert.equal(app.state.history[0].content, "고친 내용");
}
"""
                )

    def test_api_failures_pause_and_keep_text_usable(self):
        for path in ["/api/speech/transcribe", "/api/chat", "/api/speech/synthesize"]:
            with self.subTest(path=path):
                self.run_javascript(
                    r"""
app.respond("""
                    + repr(path)
                    + r""", async () => ({
  ok: false, json: async () => ({ detail: "시험 오류" }),
}));
await app.start();
await app.finish();
assert.equal(app.voice.active, false);
assert.equal(app.voice.context, null);
assert.equal(app.element("error").hidden, false);
assert.match(app.element("error").textContent, /시험 오류/);
assert.equal(app.element("voice-panel").getAttribute("data-voice-phase"), "paused");
assert.equal(app.element("voice-state-icon").getAttribute("href"), "#i-pause");
assert.equal(app.element("voice-input-meter").getAttribute("data-level"), "0");
assert.equal(app.state.history.length, """
                    + ("2" if path.endswith("synthesize") else "0")
                    + r""");
app.element("mode-text").click();
await app.send("글로 다시 물어봐요.");
assert.equal(app.state.history.at(-2).content, "글로 다시 물어봐요.");
assert.equal(app.element("message").disabled, false);
"""
                )

    def test_permission_and_recorder_and_decode_failures_pause_politely(self):
        for stage in ["permission", "recorder", "decode", "suspended"]:
            with self.subTest(stage=stage):
                self.run_javascript(
                    r"""
const stage = """
                    + repr(stage)
                    + r""";
app.element("mode-voice").click();
await app.flush();
if (stage === "permission") {
  const error = new Error("denied");
  error.name = "NotAllowedError";
  app.permissions[0].reject(error);
} else {
  await app.grant();
  if (stage === "recorder") app.recorders[0].dispatchEvent(new Event("error"));
  if (stage === "decode") {
    app.setDecode(async () => { throw new Error("decode failed"); });
    await app.finish();
  }
  if (stage === "suspended") {
    app.contexts[0].state = "suspended";
    await app.finish();
  }
}
await app.flush();
assert.equal(app.voice.active, false);
assert.equal(app.voice.context, null);
assert.equal(app.voice.stream, null);
assert.equal(app.state.busy, false);
assert.equal(app.element("mode-text").disabled, false);
assert.equal(app.outputs.length, 0);
if (stage === "permission") {
  assert.equal(app.voice.permissionPending, false);
  assert.equal(app.element("voice-start").disabled, false);
  assert.match(app.element("voice-status").textContent, /마이크 사용이 허용되지 않았어요/);
  assert.doesNotMatch(app.element("voice-status").textContent, /열려 있는 마이크 허용 창/);
}
"""
                )

    def test_emergency_advice_speaks_but_does_not_restart_listening(self):
        self.run_javascript(r"""
app.respond("/api/chat", async () => app.json(
  app.answer("지금 119에 연락해 주세요.", "emergency"),
));
await app.start();
await app.finish();
assert.equal(app.voice.phase, "speaking");
app.outputs[0].end();
await app.flush();
assert.equal(app.voice.active, false);
assert.equal(app.permissions.length, 1);
assert.match(app.element("voice-status").textContent, /119/);
assert.equal(app.state.history.length, 2);
""")

    def test_unavailable_voice_is_disabled_with_explanation(self):
        for settings in [
            "{ voiceEnabled: false }",
            "{ noAudio: true }",
            "{ noRecorder: true }",
            "{ noMicrophone: true }",
            "{ unsupportedFormat: true }",
        ]:
            with self.subTest(settings=settings):
                self.run_javascript(
                    r"""
assert.equal(app.element("mode-voice").disabled, true);
assert.match(app.element("mode-description").textContent, /글로/);
app.element("mode-voice").click();
assert.equal(app.permissions.length, 0);
assert.equal(app.contexts.length, 0);
await app.send("글로 보낼게요.");
assert.equal(app.state.history.length, 2);
""",
                    settings,
                )

    def test_manual_audio_delayed_blob_respects_cancellation(self):
        for cancellation in ["close", "switch", "reset", "pagehide", "hidden"]:
            with self.subTest(cancellation=cancellation):
                self.run_javascript(
                    r"""
const body = deferred();
app.respond("/api/speech/synthesize", async () => ({ ok: true, blob: () => body.promise }));
const speaking = app.speak("수동으로 답변 듣기");
await app.flush();
const cancellation = """
                    + repr(cancellation)
                    + r""";
if (cancellation === "close") app.element("close-audio").click();
else if (cancellation === "switch") app.element("mode-voice").click();
else app.cancel(cancellation);
body.resolve(new Blob(["late audio"]));
await speaking;
assert.equal(app.plays(), 0);
assert.equal(app.createdURLs.length, 0);
assert.equal(app.state.speaking, false);
"""
                )

    def test_pending_text_chat_reset_cannot_repopulate_new_history(self):
        self.run_javascript(r"""
const chat = deferred();
app.respond("/api/chat", () => chat.promise);
const send = app.send("진행 중 질문");
await app.flush();
assert.equal(app.element("new-chat").disabled, false);
app.element("new-chat-mobile").click();
chat.resolve(app.json(app.answer("늦은 답변")));
await send;
assert.equal(app.state.history.length, 0);
assert.equal(app.state.busy, false);
assert.equal(app.element("messages").childNodes.length, 0);
""")

    def test_switching_to_voice_aborts_pending_text_but_preserves_accepted_history(self):
        self.run_javascript(r"""
await app.send("이미 답을 받은 질문");
const delayed = deferred();
app.respond("/api/chat", () => delayed.promise);
const send = app.send("아직 답변 중인 질문");
await app.flush();
const pendingRequest = app.requests.at(-1);
app.element("mode-voice").click();
await app.flush();
assert.equal(pendingRequest.options.signal.aborted, true);
assert.equal(app.contexts.length, 1);
assert.equal(app.permissions.length, 1);
delayed.resolve(app.json(app.answer("취소한 답변")));
await send;
assert.equal(app.state.history.length, 2);
assert.equal(app.state.busy, false);
await app.grant();
assert.equal(app.voice.phase, "listening");
""")
