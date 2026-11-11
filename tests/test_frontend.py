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

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Element extends EventTarget {
  constructor() {
    super();
    this.checked = false;
    this.disabled = false;
    this.hidden = true;
    this.value = "";
    this.textContent = "";
    this.style = {};
    this.childNodes = [];
    this.lastChild = {};
    this.attributes = new Map();
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
  cloneNode() { return new Element(); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  replaceChildren(...children) { this.childNodes = children; }
  scrollIntoView() {}
  focus() {}
  pause() {}
  load() {}
  click() { if (!this.disabled) this.dispatchEvent(new Event("click")); }
}

function fixture() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const permissions = [];
  const audioBody = deferred();
  const audioRequested = deferred();
  const createdURLs = [];
  const timers = new Map();
  const lifecycle = new EventTarget();
  let playCalls = 0;
  for (const id of ["record", "stt-toggle", "tts-toggle"]) element(id).disabled = true;
  class Recorder extends EventTarget {
    static isTypeSupported() { return true; }
    constructor(stream) {
      super();
      this.stream = stream;
      this.state = "inactive";
    }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      queueMicrotask(() => this.dispatchEvent(new Event("stop")));
    }
  }
  element("audio").play = async () => { playCalls += 1; };
  const context = vm.createContext({
    console, Blob, FormData, Event, MediaRecorder: Recorder,
    document: {
      getElementById: element,
      querySelectorAll: () => [],
      body: new Element(),
    },
    navigator: {
      userAgent: "frontend-test", platform: "Linux", maxTouchPoints: 0,
      mediaDevices: {
        getUserMedia() {
          const permission = deferred();
          permissions.push(permission);
          return permission.promise;
        },
      },
    },
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() {} },
    addEventListener: lifecycle.addEventListener.bind(lifecycle),
    setTimeout(callback, delay) {
      const token = {};
      timers.set(token, { callback, delay });
      return token;
    },
    clearTimeout: (token) => timers.delete(token),
    AbortSignal: { timeout() {} },
    URL: {
      createObjectURL(blob) { createdURLs.push(blob); return "blob:fixture"; },
      revokeObjectURL() {},
    },
    fetch: async (path) => {
      if (path === "/api/config") {
        return {
          ok: true,
          json: async () => ({
            voice_enabled: true, search_enabled: true, health_info_enabled: false,
          }),
        };
      }
      if (path === "/api/speech/synthesize") {
        return {
          ok: true,
          blob() { audioRequested.resolve(); return audioBody.promise; },
        };
      }
      assert.fail(`Unexpected API request: ${path}`);
    },
  });
  vm.runInContext(fs.readFileSync("silvercare/static/app.js", "utf8"), context);
  return {
    element, permissions, audioBody, audioRequested, createdURLs, timers, lifecycle,
    state: vm.runInContext("state", context),
    startRecording: vm.runInContext("startRecording", context),
    stopRecording: vm.runInContext("stopRecording", context),
    stopAudio: vm.runInContext("stopAudio", context),
    speakText: vm.runInContext("speakText", context),
    setBusy: vm.runInContext("setBusy", context),
    playCalls: () => playCalls,
    change(id, checked) {
      element(id).checked = checked;
      element(id).dispatchEvent(new Event("change"));
    },
    stream() {
      const track = { stops: 0, stop() { this.stops += 1; } };
      return { track, getTracks: () => [track] };
    },
  };
}

async function run(test) {
  const app = fixture();
  try {
    await new Promise(setImmediate);
    await test(app);
  } finally {
    app.stopRecording(true);
    app.stopAudio();
  }
}
"""


@unittest.skipUnless(NODE, "Node.js is required for frontend regression tests")
class FrontendVoiceTests(unittest.TestCase):
    def run_javascript(self, scenario):
        script = (
            HARNESS
            + "\nrun(async (app) => {\n"
            + scenario
            + r"""
}).then(
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

    def test_delayed_audio_body_respects_cancellation(self):
        cancellations = {
            "voice_off": 'app.change("tts-toggle", false);',
            "close_audio": 'app.element("close-audio").click();',
            "new_chat": 'app.element("new-chat").click();',
            "pagehide": 'app.lifecycle.dispatchEvent(new Event("pagehide"));',
        }
        for name, cancel in cancellations.items():
            with self.subTest(cancel=name):
                self.run_javascript(
                    r"""
app.change("tts-toggle", true);
const speaking = app.speakText("private fixture answer");
await app.audioRequested.promise;
"""
                    + cancel
                    + r"""
app.audioBody.resolve(new Blob(["fixture audio"]));
await speaking;
assert.equal(app.playCalls(), 0, "Canceled audio must not play");
assert.equal(app.createdURLs.length, 0, "Canceled audio must not create an object URL");
assert.equal(app.element("audio-panel").hidden, true);
assert.equal(app.state.audioURL, null);
assert.equal(app.state.speaking, false);
"""
                )

    def test_pending_microphone_permission_is_invalidated(self):
        cancellations = {
            "new_chat": 'app.element("new-chat").click();',
            "voice_off_on": ('app.change("stt-toggle", false); app.change("stt-toggle", true);'),
            "pagehide": 'app.lifecycle.dispatchEvent(new Event("pagehide"));',
        }
        for name, cancel in cancellations.items():
            with self.subTest(cancel=name):
                self.run_javascript(
                    r"""
app.change("stt-toggle", true);
const recording = app.startRecording();
assert.equal(app.permissions.length, 1);
"""
                    + cancel
                    + r"""
const staleStream = app.stream();
app.permissions[0].resolve(staleStream);
await recording;
assert.equal(staleStream.track.stops, 1, "Canceled microphone stream must be stopped");
assert.equal(app.state.stream, null);
assert.equal(app.state.recorder, null);
assert.equal(app.element("record").classList.contains("recording"), false);
assert.equal(app.timers.size, 0, "Canceled permission must not schedule recording");
"""
                )

    def test_repeated_clicks_cannot_overlap_permission_requests(self):
        self.run_javascript(r"""
app.change("stt-toggle", true);
const recording = app.startRecording();
app.element("new-chat").click();
app.change("stt-toggle", false);
app.change("stt-toggle", true);
app.setBusy(false);
assert.equal(
  app.element("record").disabled, true, "Pending permission must keep the button disabled",
);
app.element("record").click();
await app.startRecording();
assert.equal(app.permissions.length, 1, "Only one permission request may be pending");
const staleStream = app.stream();
app.permissions[0].resolve(staleStream);
await recording;
assert.equal(staleStream.track.stops, 1);
assert.equal(app.element("record").disabled, false);
const nextRecording = app.startRecording();
assert.equal(app.permissions.length, 2, "A fresh request is allowed after cancellation settles");
const freshStream = app.stream();
app.permissions[1].resolve(freshStream);
await nextRecording;
assert.equal(app.state.recorder.state, "recording");
assert.equal(app.state.stream, freshStream);
app.stopRecording(true);
assert.equal(freshStream.track.stops, 1);
""")

    def test_voice_remains_opt_in(self):
        self.run_javascript(r"""
assert.equal(app.element("stt-toggle").checked, false);
assert.equal(app.element("tts-toggle").checked, false);
app.element("record").click();
assert.equal(app.permissions.length, 0, "Microphone use requires opt-in");
assert.equal(app.playCalls(), 0);
assert.equal(app.createdURLs.length, 0);
app.change("stt-toggle", true);
const recording = app.startRecording();
const stream = app.stream();
app.permissions[0].resolve(stream);
await recording;
assert.equal(app.state.recorder.state, "recording");
assert.equal(app.timers.size, 1);
app.stopRecording(true);
assert.equal(stream.track.stops, 1);
app.change("tts-toggle", true);
const speaking = app.speakText("opted-in fixture answer");
await app.audioRequested.promise;
app.audioBody.resolve(new Blob(["fixture audio"]));
await speaking;
assert.equal(app.playCalls(), 1);
assert.equal(app.createdURLs.length, 1);
assert.equal(app.element("audio-panel").hidden, false);
""")
