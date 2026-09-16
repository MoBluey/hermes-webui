"""Regression coverage for hands-free voice in Android WebView."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
import json
import urllib.request
from pathlib import Path

import pytest

from tests._pytest_port import BASE


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "voice_mode_media.js"


def _run_node(body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the voice-mode JavaScript tests")
    proc = subprocess.run(
        [node, "-e", textwrap.dedent(body)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_media_capture_transcribes_after_speech_and_silence() -> None:
    _run_node(
        f"""
        const assert = require('assert');
        const createCapture = require({str(MODULE)!r});

        let now = 0;
        let tick = null;
        let stoppedTracks = 0;
        let contextClosed = 0;
        let transcript = null;
        let request = null;
        let recorder = null;
        const levels = [0.009, 0.009, 0, 0, 0];
        const stream = {{getTracks: () => [{{stop: () => stoppedTracks++}}]}};

        class FakeRecorder {{
          constructor(input, options) {{
            assert.strictEqual(input, stream);
            this.mimeType = options.mimeType;
            this.state = 'inactive';
            recorder = this;
          }}
          start() {{ this.state = 'recording'; }}
          stop() {{
            this.state = 'inactive';
            this.ondataavailable({{data: new Blob(['voice'], {{type: this.mimeType}})}});
            this.onstop();
          }}
          static isTypeSupported() {{ return true; }}
        }}
        class FakeAudioContext {{
          createAnalyser() {{
            return {{
              fftSize: 0,
              getFloatTimeDomainData(buffer) {{ buffer.fill(levels.shift() || 0); }},
              disconnect() {{}},
            }};
          }}
          createMediaStreamSource() {{ return {{connect() {{}}, disconnect() {{}}}}; }}
          close() {{ contextClosed++; return Promise.resolve(); }}
        }}

        const capture = createCapture({{
          mediaDevices: {{getUserMedia: async () => stream}},
          MediaRecorder: FakeRecorder,
          AudioContext: FakeAudioContext,
          fetch: async (url, options) => {{
            request = {{url, options}};
            return {{ok: true, json: async () => ({{text: 'hello Hermes'}})}};
          }},
          now: () => now,
          setInterval: (fn) => {{ tick = fn; return 1; }},
          clearInterval: () => {{ tick = null; }},
          getSilenceMs: () => 200,
          getSessionId: () => 'session-a',
          onTranscript: (text, sid) => {{ transcript = {{text, sid}}; }},
          onError: (error) => {{ throw error; }},
        }});

        (async () => {{
          await capture.start('session-a');
          assert.strictEqual(recorder.state, 'recording');
          for (const time of [50, 180, 250, 390]) {{ now = time; tick(); }}
          await new Promise(resolve => setImmediate(resolve));
          assert.strictEqual(request.url, 'api/transcribe');
          assert.strictEqual(request.options.method, 'POST');
          assert.deepStrictEqual(transcript, {{text: 'hello Hermes', sid: 'session-a'}});
          assert.strictEqual(stoppedTracks, 1);
          assert.strictEqual(contextClosed, 1);
        }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
        """
    )


def test_media_capture_cancel_aborts_pending_transcript_and_discards_result() -> None:
    _run_node(
        f"""
        const assert = require('assert');
        const createCapture = require({str(MODULE)!r});

        let resolveRequest;
        let tick = null;
        let transcriptCount = 0;
        let requestSignal = null;
        const stream = {{getTracks: () => [{{stop() {{}}}}]}};
        class FakeRecorder {{
          constructor() {{ this.mimeType = 'audio/webm'; this.state = 'inactive'; }}
          start() {{ this.state = 'recording'; }}
          stop() {{
            this.state = 'inactive';
            this.ondataavailable({{data: new Blob(['voice'], {{type: this.mimeType}})}});
            this.onstop();
          }}
          static isTypeSupported() {{ return true; }}
        }}
        class FakeAudioContext {{
          createAnalyser() {{
            return {{fftSize: 0, getFloatTimeDomainData(buffer) {{buffer.fill(0.03);}}, disconnect() {{}}}};
          }}
          createMediaStreamSource() {{ return {{connect() {{}}, disconnect() {{}}}}; }}
          close() {{ return Promise.resolve(); }}
        }}
        const capture = createCapture({{
          mediaDevices: {{getUserMedia: async () => stream}},
          MediaRecorder: FakeRecorder,
          AudioContext: FakeAudioContext,
          fetch: (_url, options) => {{
            requestSignal = options.signal;
            return new Promise(resolve => {{ resolveRequest = resolve; }});
          }},
          setInterval: (fn) => {{ tick = fn; return 1; }},
          clearInterval: () => {{ tick = null; }},
          now: (() => {{ let value = 0; return () => value += 250; }})(),
          getSilenceMs: () => 200,
          getSessionId: () => 'session-a',
          onTranscript: () => transcriptCount++,
          onError: (error) => {{ throw error; }},
        }});

        (async () => {{
          await capture.start('session-a');
          tick();
          capture.finish();
          await new Promise(resolve => setImmediate(resolve));
          assert.ok(requestSignal);
          capture.cancel();
          assert.strictEqual(requestSignal.aborted, true);
          resolveRequest({{ok: true, json: async () => ({{text: 'ghost turn'}})}});
          await new Promise(resolve => setImmediate(resolve));
          assert.strictEqual(transcriptCount, 0);
        }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
        """
    )


def test_media_capture_restarts_after_empty_result_and_stops_on_session_change() -> None:
    _run_node(
        f"""
        const assert = require('assert');
        const createCapture = require({str(MODULE)!r});
        let sid = 'session-a';
        let tick = null;
        let emptyCount = 0;
        let sessionChangeCount = 0;
        let stoppedTracks = 0;
        const stream = {{getTracks: () => [{{stop: () => stoppedTracks++}}]}};
        class FakeRecorder {{
          constructor() {{ this.mimeType = 'audio/webm'; this.state = 'inactive'; }}
          start() {{ this.state = 'recording'; }}
          stop() {{
            this.state = 'inactive';
            this.ondataavailable({{data: new Blob(['voice'], {{type: this.mimeType}})}});
            this.onstop();
          }}
          static isTypeSupported() {{ return true; }}
        }}
        class FakeAudioContext {{
          createAnalyser() {{
            return {{fftSize: 0, getFloatTimeDomainData(buffer) {{buffer.fill(0);}}, disconnect() {{}}}};
          }}
          createMediaStreamSource() {{ return {{connect() {{}}, disconnect() {{}}}}; }}
          close() {{ return Promise.resolve(); }}
        }}
        const capture = createCapture({{
          mediaDevices: {{getUserMedia: async () => stream}},
          MediaRecorder: FakeRecorder,
          AudioContext: FakeAudioContext,
          fetch: async () => ({{ok: true, json: async () => ({{transcript: ''}})}}),
          setInterval: fn => {{ tick = fn; return 1; }},
          clearInterval: () => {{ tick = null; }},
          getSessionId: () => sid,
          onEmpty: capturedSid => {{ assert.strictEqual(capturedSid, 'session-a'); emptyCount++; }},
          onSessionChange: capturedSid => {{ assert.strictEqual(capturedSid, 'session-a'); sessionChangeCount++; }},
          onError: error => {{ throw error; }},
        }});
        (async () => {{
          await capture.start('session-a');
          capture.finish();
          await new Promise(resolve => setImmediate(resolve));
          assert.strictEqual(emptyCount, 1);
          await capture.start('session-a');
          sid = 'session-b';
          tick();
          assert.strictEqual(sessionChangeCount, 1);
          assert.strictEqual(stoppedTracks, 2);
        }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
        """
    )


def test_boot_loads_media_fallback_before_hands_free_controller() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert html.index("static/voice_mode_media.js") < html.index("static/boot.js")
    assert "window.createHermesVoiceModeMediaCapture" in boot
    assert "if(!hasSTT||!hasTTS) return;" not in boot
    assert "mic_force_mediarecorder" in boot
    assert "_voiceModeCanSpeak" in boot
    assert "_voiceModeRunIsCurrent(voiceGeneration,speakingSid)" in boot


def test_media_fallback_asset_is_served(cleanup_test_sessions) -> None:
    with urllib.request.urlopen(BASE + "/static/voice_mode_media.js", timeout=10) as response:
        source = response.read().decode("utf-8")
    assert response.status == 200
    assert "createHermesVoiceModeMediaCapture" in source


def test_session_and_profile_navigation_end_voice_mode() -> None:
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "currentSid!==sid&&typeof window._voiceModeActive==='function'" in sessions
    assert "window._voiceModeActive()" in sessions
    assert "window._voiceModeDeactivate();" in sessions
    assert "name === S.activeProfile) return true;" in panels
    assert "typeof window._voiceModeActive === 'function'" in panels
    new_session = sessions[sessions.index("async function newSession(") :]
    in_flight_guard = new_session.index("if(_newSessionInFlight){")
    deactivate = new_session.index("S.session&&typeof window._voiceModeActive==='function'")
    pending = new_session.index("_setNewSessionPending(true);")
    assert in_flight_guard < deactivate < pending
    assert "typeof window._voiceModeDeactivate === 'function'" in panels


def test_webview_voice_controller_transcribes_speaks_and_relistens() -> None:
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    marker = "// ── Turn-based voice mode (#1333)"
    start = boot.index("(function(){", boot.index(marker))
    end = boot.index("\n})();", start) + len("\n})();")
    controller = boot[start:end]
    _run_node(
        f"""
        const assert = require('assert');
        const controller = {json.dumps(controller)};
        const elements = {{}};
        function element(id) {{
          return elements[id] ||= {{
            id,
            style: {{}},
            dataset: {{}},
            value: '',
            classList: {{add() {{}}, remove() {{}}}},
          }};
        }}
        global.window = global;
        global.location = {{href: 'https://hermes.test/', protocol: 'https:', hostname: 'hermes.test'}};
        window.location = location;
        window.isSecureContext = true;
        global.document = {{
          baseURI: 'https://hermes.test/',
          querySelectorAll: () => [{{dataset: {{rawText: 'Hello from Hermes'}}}}],
        }};
        global.$ = element;
        const settings = new Map([
          ['hermes-voice-mode-button', 'true'],
          ['hermes-tts-engine', 'openai'],
          ['mic_force_mediarecorder', '1'],
        ]);
        global.localStorage = {{getItem: key => settings.get(key) ?? null}};
        global.t = key => key;
        const toasts = [];
        global.showToast = message => toasts.push(message);
        global._setButtonTooltip = () => {{}};
        global._micOriginNeedsSecureContext = () => false;
        global.autoResize = () => {{}};
        let stopTtsCount = 0;
        global.stopTTS = () => {{
          stopTtsCount++;
          if(global._playingEdgeAudio) {{
            global._playingEdgeAudio.stop();
            global._playingEdgeAudio.disconnect();
            global._playingEdgeAudio = null;
          }}
        }};
        global.S = {{busy: false, session: {{session_id: 'session-a'}}}};
        global._ttsSpeaking = false;
        global._playingEdgeAudio = null;
        let sendCount = 0;
        global.send = () => sendCount++;
        const pendingEmptyTimers = [];
        global.setTimeout = (fn, delay) => {{
          if(delay === 300) pendingEmptyTimers.push(fn);
          else fn();
          return 1;
        }};
        global.clearTimeout = () => {{}};
        global.setInterval = () => 1;
        global.clearInterval = () => {{}};
        let captureOptions;
        let listenCount = 0;
        const listenedSessions = [];
        let cancelCount = 0;
        window.createHermesVoiceModeMediaCapture = options => {{
          captureOptions = options;
          return {{
            supported: () => true,
            start: async sid => {{ listenCount++; listenedSessions.push(sid); }},
            cancel: () => cancelCount++,
          }};
        }};
        let requestedUrl = null;
        global.fetch = async (url, options) => {{
          requestedUrl = url;
          assert.strictEqual(options.method, 'POST');
          return {{ok: true, arrayBuffer: async () => new ArrayBuffer(8)}};
        }};
        let inGesture = false;
        let resumeCount = 0;
        let source = null;
        let failResume = false;
        const audioContext = {{
          state: 'suspended',
          destination: {{}},
          resume() {{
            assert.strictEqual(inGesture, true, 'AudioContext must be resumed by the voice-button gesture');
            resumeCount++;
            if(failResume) return Promise.reject(new Error('playback denied'));
            this.state = 'running';
            return Promise.resolve();
          }},
          decodeAudioData(_buffer, success, failure) {{
            success({{decoded: true}});
          }},
          createBufferSource() {{
            source = {{
              connect() {{}},
              disconnect() {{ this.disconnected = true; }},
              start() {{ this.played = true; }},
              stop() {{ this.stopped = true; }},
            }};
            return source;
          }},
        }};
        global._getTtsAudioCtx = () => audioContext;
        const clickVoice = button => {{
          inGesture = true;
          button.onclick();
          inGesture = false;
        }};

        eval(controller);
        (async () => {{
          const button = element('btnVoiceMode');
          assert.strictEqual(button.style.display, '');
          clickVoice(button);
          assert.strictEqual(resumeCount, 1);
          assert.strictEqual(listenCount, 1);
          captureOptions.onTranscript('hello Hermes', 'session-a');
          assert.strictEqual(element('msg').value, 'hello Hermes');
          assert.strictEqual(sendCount, 1);
          assert.ok(cancelCount >= 1);
          window.autoReadLastAssistant();
          await new Promise(resolve => setImmediate(resolve));
          await new Promise(resolve => setImmediate(resolve));
          assert.strictEqual(requestedUrl, 'https://hermes.test/api/tts');
          assert.strictEqual(source.played, true);
          assert.strictEqual(listenCount, 1);
          source.onended();
          assert.strictEqual(listenCount, 2);
          captureOptions.onEmpty('session-a');
          assert.strictEqual(pendingEmptyTimers.length, 1);
          clickVoice(button);
          clickVoice(button);
          assert.strictEqual(listenCount, 3);
          pendingEmptyTimers.shift()();
          assert.strictEqual(listenCount, 3);
          captureOptions.onTranscript('second turn', 'session-a');
          assert.strictEqual(sendCount, 2);
          window.autoReadLastAssistant();
          await new Promise(resolve => setImmediate(resolve));
          await new Promise(resolve => setImmediate(resolve));
          const staleAudio = source;
          const staleEnded = staleAudio.onended;
          clickVoice(button);
          clickVoice(button);
          assert.strictEqual(listenCount, 4);
          assert.strictEqual(staleAudio.stopped, true);
          assert.strictEqual(staleAudio.disconnected, true);
          staleEnded();
          assert.strictEqual(listenCount, 4);
          clickVoice(button);
          audioContext.state = 'suspended';
          failResume = true;
          clickVoice(button);
          assert.strictEqual(listenCount, 5);
          captureOptions.onTranscript('playback failure', 'session-a');
          assert.strictEqual(sendCount, 3);
          window.autoReadLastAssistant();
          await new Promise(resolve => setImmediate(resolve));
          await new Promise(resolve => setImmediate(resolve));
          assert.ok(toasts.some(message => message.includes('OpenAI TTS error: playback denied')));
          assert.strictEqual(listenCount, 6);
          clickVoice(button);
          captureOptions.onTranscript('ghost turn', 'session-a');
          assert.strictEqual(sendCount, 3);
          failResume = false;
          audioContext.state = 'running';
          clickVoice(button);
          assert.strictEqual(listenCount, 7);
          S.session.session_id = 'session-b';
          captureOptions.onSessionChange('session-a');
          captureOptions.onTranscript('wrong session', 'session-a');
          assert.strictEqual(sendCount, 3);
          assert.strictEqual(window._voiceModeActive(), false);
          assert.deepStrictEqual(listenedSessions, ['session-a', 'session-a', 'session-a', 'session-a', 'session-a', 'session-a', 'session-a']);
        }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
        """
    )
