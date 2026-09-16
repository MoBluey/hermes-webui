/* MediaRecorder-based hands-free input for WebViews without Web Speech STT. */
(function(root, factory){
  const create=factory(root);
  if(typeof module==='object'&&module.exports) module.exports=create;
  if(root) root.createHermesVoiceModeMediaCapture=create;
})(typeof globalThis!=='undefined'?globalThis:this, function(root){
  'use strict';

  return function createHermesVoiceModeMediaCapture(options){
    const opts=options||{};
    const mediaDevices=opts.mediaDevices||(root.navigator&&root.navigator.mediaDevices);
    const Recorder=opts.MediaRecorder||root.MediaRecorder;
    const AudioContextCtor=opts.AudioContext||root.AudioContext||root.webkitAudioContext;
    const fetchFn=opts.fetch||root.fetch;
    const now=opts.now||Date.now;
    const setIntervalFn=opts.setInterval||root.setInterval.bind(root);
    const clearIntervalFn=opts.clearInterval||root.clearInterval.bind(root);
    const getSilenceMs=opts.getSilenceMs||function(){ return 1800; };
    const getSessionId=opts.getSessionId||function(){ return null; };
    const onTranscript=opts.onTranscript||function(){};
    const onEmpty=opts.onEmpty||function(){};
    const onSessionChange=opts.onSessionChange||function(){};
    const onError=opts.onError||function(){};
    const levelThreshold=Number.isFinite(opts.levelThreshold)?opts.levelThreshold:0.008;
    const minSpeechMs=Number.isFinite(opts.minSpeechMs)?opts.minSpeechMs:120;
    let generation=0;
    let run=null;

    function supported(){
      return !!(mediaDevices&&typeof mediaDevices.getUserMedia==='function'&&Recorder&&AudioContextCtor&&fetchFn);
    }

    function chooseMimeType(){
      const choices=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus'];
      if(typeof Recorder.isTypeSupported!=='function') return '';
      return choices.find(function(type){ return Recorder.isTypeSupported(type); })||'';
    }

    function stopMedia(active){
      if(active.interval){
        clearIntervalFn(active.interval);
        active.interval=null;
      }
      try{ if(active.source) active.source.disconnect(); }catch(_){}
      try{ if(active.analyser) active.analyser.disconnect(); }catch(_){}
      if(active.stream){
        active.stream.getTracks().forEach(function(track){
          try{ track.stop(); }catch(_){}
        });
        active.stream=null;
      }
      if(active.context){
        try{ void active.context.close(); }catch(_){}
        active.context=null;
      }
    }

    async function transcribe(active){
      stopMedia(active);
      if(active.cancelled||active.token!==generation) return;
      if(!active.shouldTranscribe){
        if(getSessionId()===active.sessionId) onEmpty(active.sessionId);
        if(run===active) run=null;
        return;
      }
      const blob=new Blob(active.chunks,{type:active.mimeType||'audio/webm'});
      if(!blob.size){
        if(getSessionId()===active.sessionId) onEmpty(active.sessionId);
        if(run===active) run=null;
        return;
      }
      const ext=blob.type&&blob.type.includes('ogg')?'ogg':'webm';
      const form=new FormData();
      form.append('file',new File([blob],'voice-input.'+ext,{type:blob.type||'audio/'+ext}));
      const aborter=new AbortController();
      active.aborter=aborter;
      try{
        const response=await fetchFn('api/transcribe',{method:'POST',body:form,signal:aborter.signal});
        const data=await response.json().catch(function(){ return {}; });
        if(!response.ok) throw new Error(data.error||'Transcription failed');
        if(active.cancelled||active.token!==generation) return;
        if(getSessionId()!==active.sessionId){
          onSessionChange(active.sessionId);
          return;
        }
        const text=String(data.transcript||data.text||'').trim();
        if(text) onTranscript(text,active.sessionId);
        else onEmpty(active.sessionId);
      }catch(error){
        if(error&&error.name==='AbortError') return;
        if(!active.cancelled&&active.token===generation) onError(error);
      }finally{
        if(run===active) run=null;
      }
    }

    function finish(){
      const active=run;
      if(!active||active.cancelled) return;
      active.shouldTranscribe=true;
      if(active.recorder&&active.recorder.state!=='inactive') active.recorder.stop();
    }

    function cancel(){
      generation++;
      const active=run;
      run=null;
      if(!active) return;
      active.cancelled=true;
      if(active.aborter) active.aborter.abort();
      stopMedia(active);
      try{
        if(active.recorder&&active.recorder.state!=='inactive') active.recorder.stop();
      }catch(_){}
    }

    async function start(sessionId){
      cancel();
      if(!supported()) throw new Error('MediaRecorder voice capture is unavailable');
      const active={
        token:++generation,
        sessionId:sessionId,
        stream:null,
        recorder:null,
        context:null,
        source:null,
        analyser:null,
        interval:null,
        aborter:null,
        chunks:[],
        mimeType:'',
        cancelled:false,
        shouldTranscribe:false,
        loudSince:null,
        lastLoudAt:null,
        speechStarted:false,
      };
      run=active;
      let stream;
      try{
        stream=await mediaDevices.getUserMedia({audio:true});
        if(active.cancelled||active.token!==generation){
          stream.getTracks().forEach(function(track){ track.stop(); });
          return;
        }
        active.stream=stream;
        const mimeType=chooseMimeType();
        active.mimeType=mimeType;
        active.recorder=new Recorder(stream,mimeType?{mimeType:mimeType}:undefined);
        active.recorder.ondataavailable=function(event){
          if(event.data&&event.data.size>0) active.chunks.push(event.data);
        };
        active.recorder.onstop=function(){ void transcribe(active); };
        active.recorder.onerror=function(event){
          stopMedia(active);
          if(!active.cancelled&&active.token===generation) onError(event.error||new Error('Audio recording failed'));
        };

        active.context=new AudioContextCtor();
        if(active.context.state==='suspended'&&typeof active.context.resume==='function'){
          void active.context.resume();
        }
        active.source=active.context.createMediaStreamSource(stream);
        active.analyser=active.context.createAnalyser();
        active.analyser.fftSize=1024;
        active.source.connect(active.analyser);
        const samples=new Float32Array(active.analyser.fftSize);
        active.interval=setIntervalFn(function(){
          if(active.cancelled||active.token!==generation) return;
          if(getSessionId()!==active.sessionId){
            cancel();
            onSessionChange(active.sessionId);
            return;
          }
          active.analyser.getFloatTimeDomainData(samples);
          let sum=0;
          for(let i=0;i<samples.length;i++) sum+=samples[i]*samples[i];
          const rms=Math.sqrt(sum/samples.length);
          const time=now();
          if(rms>=levelThreshold){
            if(active.loudSince===null) active.loudSince=time;
            active.lastLoudAt=time;
            if(time-active.loudSince>=minSpeechMs) active.speechStarted=true;
            return;
          }
          active.loudSince=null;
          if(active.speechStarted&&active.lastLoudAt!==null&&time-active.lastLoudAt>=getSilenceMs()) finish();
        },100);
        active.recorder.start();
      }catch(error){
        stopMedia(active);
        if(run===active) run=null;
        if(!active.cancelled&&active.token===generation) onError(error);
      }
    }

    return {supported:supported,start:start,finish:finish,cancel:cancel};
  };
});
