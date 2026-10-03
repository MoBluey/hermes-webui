"""Incremental speech delivery, bounded lookahead and playback ownership."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import api.routes as routes
from tests.test_issue4982_openai_tts import _FakeHandler, _StreamOnceResponse
from tests.test_issue4755_profile_default_workspace import _extract_async_function


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def streaming_provider(monkeypatch):
    import api.auth as auth
    import api.config as config

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "test-local-voice")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "http://127.0.0.1:8891/v1",
                            "model": "local-voice", "voice": "local",
                            "streaming": True, "sample_rate": 24000}}
    })
    monkeypatch.delattr(routes._handle_tts, "_tts_limiter", raising=False)
    yield
    monkeypatch.delattr(routes._handle_tts, "_tts_limiter", raising=False)


def _handler():
    return _FakeHandler(json.dumps({"text": "Hello", "engine": "openai", "stream": True}).encode())


def test_pcm_is_forwarded_before_upstream_finishes(streaming_provider, monkeypatch):
    handler = _handler()
    observed = []
    request = {}

    class Response(_StreamOnceResponse):
        def read1(self, size):
            observed.append(handler.wfile.getvalue())
            return super().read(size)

        def read(self, size=-1):
            observed.append(handler.wfile.getvalue())
            return super().read(size)

    def upstream(req, **kwargs):
        request.update(json.loads(req.data))
        return Response([b"\x01\x00", b"\x02\x00"], {"Content-Type": "audio/pcm"})

    monkeypatch.setattr(routes, "_tts_open", upstream)
    routes._handle_tts(handler, None)

    assert handler.status == 200
    assert b"\x01\x00" in observed[1], "first PCM bytes must leave before the second read"
    assert request["stream"] is True
    assert request["response_format"] == "pcm"
    assert handler.sent_headers["X-Audio-Sample-Rate"] == "24000"
    assert handler.sent_headers["X-Audio-Channels"] == "1"
    assert handler.sent_headers["Transfer-Encoding"] == "chunked"
    assert "Content-Length" not in handler.sent_headers
    assert handler.wfile.getvalue().endswith(b"0\r\n\r\n")


def test_stream_rejects_wrong_audio_format_before_headers(streaming_provider, monkeypatch):
    monkeypatch.setattr(routes, "_tts_open", lambda *a, **kw:
                        _StreamOnceResponse([b"not-pcm"], {"Content-Type": "audio/mpeg"}))
    handler = _handler()
    routes._handle_tts(handler, None)
    assert handler.status == 502


def test_stream_byte_limit_aborts_without_success_terminator(streaming_provider, monkeypatch):
    monkeypatch.setattr(routes, "_TTS_PROXY_MAX_BYTES", 2)
    monkeypatch.setattr(routes, "_tts_open", lambda *a, **kw:
                        _StreamOnceResponse([b"\x01\x00", b"\x02\x00"], {"Content-Type": "audio/pcm"}))
    handler = _handler()
    routes._handle_tts(handler, None)
    assert handler.status == 200
    assert getattr(handler, "close_connection", False) is True
    assert not handler.wfile.getvalue().endswith(b"0\r\n\r\n")
    assert b"generation failed" not in handler.wfile.getvalue()


def test_stream_disconnect_closes_upstream(streaming_provider, monkeypatch):
    response = _StreamOnceResponse([b"\x01\x00"], {"Content-Type": "audio/pcm"})
    closed = []
    response.__class__ = type("ClosingResponse", (_StreamOnceResponse,), {
        "__exit__": lambda *args: closed.append(True),
    })
    monkeypatch.setattr(routes, "_tts_open", lambda *a, **kw: response)
    handler = _handler()

    class Disconnected(io.BytesIO):
        def write(self, value):
            raise BrokenPipeError("browser stopped")

    handler.wfile = Disconnected()
    routes._handle_tts(handler, None)
    assert closed == [True]
    assert getattr(handler, "close_connection", False) is True


NODE_SETUP = r"""
const fs=require('fs');
const assert=require('assert');
global.window=global;
global.document={baseURI:'https://hermes.test/', querySelectorAll:()=>[]};
global.location={href:'https://hermes.test/'};
global.localStorage={getItem:()=>null};
global.S={session:{session_id:'session-a'}};
const toasts=[];
global.showToast=message=>toasts.push(message);
let clock=0;
Date.now=()=>clock;
global.setTimeout=(fn,ms)=>{clock+=ms; queueMicrotask(fn); return 1;};
global.clearTimeout=()=>{};
const sources=[];
const decoded=[];
const context={state:'running',currentTime:0,destination:{},
  resume:async()=>{},
  createBuffer:(channels,length,rate)=>{
    assert.strictEqual(channels,1);
    const data=new Float32Array(length);
    return {duration:length/rate,getChannelData:()=>data};
  },
  decodeAudioData:(data,success)=>{decoded.push(data.byteLength);success({duration:4});},
  createBufferSource:()=>{
    const source={connect(){},disconnect(){this.disconnected=true;},
      start(when){this.when=when;this.played=true;},stop(){this.stopped=true;}};
    sources.push(source);
    return source;
  },
};
global.AudioContext=function(){return context;};
const asset=root+'/static/tts_stream.js';
if(fs.existsSync(asset)) require(asset);
const ui=fs.readFileSync(root+'/static/ui.js','utf8');
eval(ui.slice(ui.indexOf('// ── TTS: Text-to-Speech'),ui.indexOf('function autoReadLastAssistant()')));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const bodyHeaders={'Content-Type':'audio/pcm','X-Audio-Format':'pcm_s16le',
  'X-Audio-Sample-Rate':'24000','X-Audio-Channels':'1'};
const button={dataset:{}};
const requests=[];
const controllers=[];
const signals=[];
global.fetch=async(url,options)=>{
  requests.push(JSON.parse(options.body)); signals.push(options.signal);
  return new Response(new ReadableStream({
    start(controller){controllers.push(controller);},
  }),{headers:bodyHeaders});
};
"""


def _node(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for playback tests")
    script = "const root=" + json.dumps(str(ROOT)) + ";\n" + NODE_SETUP + "\n" + body
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("file,name,setup,call,noop", [
    ("sessions.js", "newSession",
     "global._newSessionInFlight=null; global._setNewSessionPending=()=>{throw boundary;};",
     "newSession(false)", None),
    ("sessions.js", "loadSession",
     "global._loadingSessionId=null; global._rearmActiveSessionStream=()=>{}; "
     "global._sessionVisitHasUnreadState=()=>false; "
     "global.syncClaudeTerminalNavigation=()=>{throw boundary;};",
     "loadSession('session-b')", "loadSession('session-a')"),
    ("panels.js", "switchToProfile",
     "S.activeProfile='default'; global.$=()=>{throw boundary;};",
     "switchToProfile('another-profile')", "switchToProfile('default')"),
])
def test_navigation_cancels_scheduled_speech_before_changing_state(file, name, setup, call, noop):
    function = _extract_async_function((ROOT / "static" / file).read_text(), name)
    _node("eval(" + json.dumps(function) + ");\n" + r"""
    (async()=>{
      const boundary=new Error('navigation boundary');
      _playOpenaiTts('Still speaking.',button);
      await flush();
      controllers[0].enqueue(new Uint8Array([0,64]));
      await flush();
    """ + setup + (f"await {noop}; assert.strictEqual(signals[0].aborted,false);" if noop else "") +
    f"\nawait assert.rejects({call},error=>error===boundary);" + r"""
      assert.strictEqual(signals[0].aborted,true,'navigation must cancel immediately');
      assert.ok(sources[0].stopped&&sources[0].disconnected);
      assert.strictEqual(button.dataset.speaking,'0');
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_browser_plays_pcm_before_response_eof_and_preserves_split_samples():
    _node(r"""
    (async()=>{
      _playOpenaiTts('Hello Jarvis.',button);
      await flush();
      assert.strictEqual(requests[0].engine,'openai');
      assert.strictEqual(requests[0].stream,true);
      controllers[0].enqueue(new Uint8Array([0,64,0]));
      await flush();
      assert.strictEqual(sources.length,1,'playback must start before EOF');
      assert.strictEqual(sources[0].buffer.getChannelData(0)[0],0.5);
      controllers[0].enqueue(new Uint8Array([192]));
      controllers[0].close();
      await flush();
      assert.strictEqual(sources[1].buffer.getChannelData(0)[0],-0.5);
      assert.strictEqual(sources[1].when,sources[0].when+1/24000);
      assert.strictEqual(button.dataset.speaking,'1');
      sources.forEach(source=>source.onended());
      await flush();
      assert.strictEqual(button.dataset.speaking,'0');
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_browser_prefetches_one_sentence_while_previous_is_playing():
    _node(r"""
    (async()=>{
      const first='First '+ 'word '.repeat(38)+'.';
      const second='Second '+ 'word '.repeat(38)+'.';
      const third='Third '+ 'word '.repeat(38)+'.';
      _playOpenaiTts([first,second,third].join(' '),button);
      await flush();
      assert.strictEqual(requests[0].text,first);
      controllers[0].enqueue(new Uint8Array(24000*2)); controllers[0].close();
      await flush(); await flush();
      assert.strictEqual(requests.length,2,'synthesise next sentence during playback');
      assert.strictEqual(requests[1].text,second);
      controllers[1].enqueue(new Uint8Array(24000*2)); controllers[1].close();
      await flush(); await flush();
      assert.strictEqual(requests.length,2,'only one sentence of lookahead');
      sources[0].onended();
      await flush(); await flush();
      assert.strictEqual(requests.length,3);
      stopTTS();
      assert.ok(signals.every(signal=>signal.aborted));
      assert.ok(sources.slice(1).every(source=>source.stopped&&source.disconnected));
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_stop_discards_a_late_fetch_response():
    _node(r"""
    (async()=>{
      let release;
      global.fetch=(_url,opts)=>{
        signals.push(opts.signal);
        return new Promise(resolve=>release=resolve);
      };
      _playOpenaiTts('Cancelled sentence.',button);
      await flush();
      stopTTS();
      assert.ok(signals[0]&&signals[0].aborted,'stop must abort its pending fetch');
      release(new Response(new Uint8Array([0,64]),{headers:bodyHeaders}));
      await flush(); await flush();
      assert.strictEqual(sources.length,0,'cancelled response must never play');
      assert.strictEqual(button.dataset.speaking,'0');
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_session_change_during_decode_cannot_play_old_reply():
    _node(r"""
    (async()=>{
      let release;
      context.decodeAudioData=(_data,success)=>{release=success;};
      global.fetch=async()=>new Response(new Uint8Array([1,2]),{headers:{'Content-Type':'audio/mpeg'}});
      _playOpenaiTts('Old session.',button);
      await flush();
      S.session={session_id:'session-b'};
      release({duration:1});
      await flush(); await flush();
      assert.strictEqual(sources.length,0,'session ownership must survive decode');
      assert.strictEqual(button.dataset.speaking,'0');
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_stream_failure_stops_scheduled_audio_and_reports_error():
    _node(r"""
    (async()=>{
      _playOpenaiTts('Failing stream.',button);
      await flush();
      controllers[0].enqueue(new Uint8Array(24000*2));
      await flush();
      assert.strictEqual(sources.length,1);
      controllers[0].error(new Error('stream interrupted'));
      await flush(); await flush();
      assert.ok(sources[0].stopped&&sources[0].disconnected);
      assert.strictEqual(button.dataset.speaking,'0');
      assert.ok(toasts.some(message=>message.includes('stream interrupted')));
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_voice_mode_prefetches_and_relistens_only_after_all_audio():
    _node(r"""
    (async()=>{
      const elements={};
      global.$=id=>elements[id]||=( {style:{},dataset:{},value:'',classList:{add(){},remove(){}}} );
      const settings=new Map([['hermes-voice-mode-button','true'],['hermes-tts-engine','openai'],
        ['mic_force_mediarecorder','1']]);
      localStorage.getItem=key=>settings.get(key)||null;
      global.t=key=>key;
      global._setButtonTooltip=()=>{};
      global._micOriginNeedsSecureContext=()=>false;
      global.autoResize=()=>{};
      global.autoReadLastAssistant=()=>{};
      let capture;
      let listens=0;
      global.createHermesVoiceModeMediaCapture=options=>{
        capture=options;
        return {supported:()=>true,start:async()=>listens++,cancel(){}};
      };
      global.createHermesVoiceSentenceTracker=require(root+'/static/voice_mode_sentences.js');
      global.send=()=>{};
      global.fetch=async(_url,options)=>{
        requests.push(JSON.parse(options.body)); signals.push(options.signal);
        return new Response(new Uint8Array([1,2]),{headers:{'Content-Type':'audio/mpeg'}});
      };
      const boot=fs.readFileSync(root+'/static/boot.js','utf8');
      const start=boot.indexOf('(function(){',boot.indexOf('// ── Turn-based voice mode (#1333)'));
      const end=boot.indexOf('\n})();',start)+'\n})();'.length;
      // Execute the shared script and its consumer in the same browser-like lexical scope.
      eval(ui.slice(ui.indexOf('// ── TTS: Text-to-Speech'),ui.indexOf('function autoReadLastAssistant()'))+
        '\n'+boot.slice(start,end));
      $('btnVoiceMode').onclick();
      capture.onTranscript('hello','session-a');
      _voiceModeOnStreamText('First sentence. Second sentence.', 'session-a','stream-a');
      _voiceModeOnStreamDone('session-a','stream-a');
      await flush(); await flush();
      assert.deepStrictEqual(requests.map(request=>request.text),['First sentence.','Second sentence.']);
      assert.ok(requests.every(request=>request.engine==='openai'&&request.stream===true));
      assert.strictEqual(listens,1,'mic must remain off during both sentences');
      sources[0].onended();
      await flush();
      assert.strictEqual(listens,1);
      sources[1].onended();
      await flush(); await flush();
      assert.strictEqual(listens,2);
      $('btnVoiceMode').onclick();
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)
