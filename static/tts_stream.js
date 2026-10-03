/* Incremental PCM playback with one sentence of synthesis lookahead. */
(function(root,factory){
  const create=factory();
  if(typeof module==='object'&&module.exports) module.exports=create;
  if(root) root.createHermesTtsPlayer=create;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  // The server admits one TTS request per client every two seconds.
  let requestAfter=0;
  const maxBytes=16*1024*1024;

  return function createHermesTtsPlayer(options){
    const context=options.context;
    const controller=new AbortController();
    const queue=[];
    const clips=new Set();
    const sources=new Set();
    let reader=null;
    let loading=false;
    let closed=false;
    let settled=false;
    let nextAudioTime=0;
    let resolveDone,rejectDone;
    const done=new Promise((resolve,reject)=>{resolveDone=resolve;rejectDone=reject;});

    function dispose(error){
      if(settled) return;
      settled=true;
      queue.length=0;
      controller.abort();
      if(reader) reader.cancel().catch(()=>{});
      sources.forEach(source=>{
        source.onended=null;
        try{source.stop();}catch(_){}
        try{source.disconnect();}catch(_){}
      });
      sources.clear();
      clips.clear();
      if(error) rejectDone(error);
      else resolveDone(false);
    }

    function current(){
      if(settled) return false;
      if(!options.isCurrent()){dispose();return false;}
      return true;
    }

    function wait(ms){
      if(ms<=0) return Promise.resolve();
      return new Promise((resolve,reject)=>{
        const signal=controller.signal;
        const abort=()=>{clearTimeout(timer);reject(new DOMException('Speech cancelled','AbortError'));};
        const timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},ms);
        signal.addEventListener('abort',abort,{once:true});
      });
    }

    async function request(text){
      for(let attempt=0;attempt<2;attempt++){
        await wait(Math.max(0,requestAfter-Date.now()));
        if(!current()) return;
        requestAfter=Date.now()+2100;
        const response=await fetch(options.url,{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({text:text,engine:'openai',stream:true}),signal:controller.signal,
        });
        if(!current()){
          if(response.body) response.body.cancel().catch(()=>{});
          return;
        }
        if(response.status===429&&attempt===0){
          if(response.body) await response.body.cancel();
          requestAfter=Date.now()+2100;
          continue;
        }
        if(!response.ok){
          const detail=await response.json().catch(()=>({}));
          throw new Error(detail.error||('TTS request failed: '+response.status));
        }
        return response;
      }
    }

    function releaseClip(clip){
      if(clip.downloaded&&clip.pending===0) clips.delete(clip);
      pump();
    }

    function schedule(buffer,clip){
      if(!current()) return;
      const source=context.createBufferSource();
      source.buffer=buffer;
      source.connect(context.destination);
      sources.add(source);
      clip.pending++;
      source.onended=()=>{
        if(!sources.delete(source)) return;
        try{source.disconnect();}catch(_){}
        clip.pending--;
        releaseClip(clip);
      };
      const when=Math.max(context.currentTime+0.02,nextAudioTime);
      nextAudioTime=when+buffer.duration;
      source.start(when);
    }

    async function load(text,clip){
      await options.unlocked;
      if(!current()) return;
      if(!context||context.state==='suspended'||context.state==='closed') throw new Error('Audio playback was blocked');
      const response=await request(text);
      if(!response||!current()) return;
      const header=name=>response.headers?response.headers.get(name):null;
      if((header('Content-Type')||'').split(';')[0].trim().toLowerCase()!=='audio/pcm'){
        const bytes=await response.arrayBuffer();
        if(!current()) return;
        if(!bytes.byteLength||bytes.byteLength>maxBytes) throw new Error('Invalid TTS audio size');
        const buffer=await new Promise((resolve,reject)=>context.decodeAudioData(bytes,resolve,reject));
        schedule(buffer,clip);
        return;
      }
      const rate=Number(header('X-Audio-Sample-Rate'));
      if(header('X-Audio-Format')!=='pcm_s16le'||header('X-Audio-Channels')!=='1'
          ||!Number.isInteger(rate)||rate<8000||rate>96000) throw new Error('Unsupported PCM audio format');
      if(!response.body||!response.body.getReader) throw new Error('Streaming audio is unavailable');
      const input=response.body.getReader();
      reader=input;
      let carry=new Uint8Array(0);
      let total=0;
      let eof=false;
      try{
        while(current()){
          const result=await input.read();
          if(!current()) return;
          if(result.done){eof=true;break;}
          total+=result.value.byteLength;
          if(total>maxBytes) throw new Error('TTS audio exceeded byte limit');
          const bytes=new Uint8Array(carry.length+result.value.length);
          bytes.set(carry);bytes.set(result.value,carry.length);
          const frames=Math.floor(bytes.length/2);
          carry=bytes.slice(frames*2);
          if(!frames) continue;
          const buffer=context.createBuffer(1,frames,rate);
          const samples=buffer.getChannelData(0);
          const data=new DataView(bytes.buffer);
          for(let index=0;index<frames;index++) samples[index]=data.getInt16(index*2,true)/32768;
          schedule(buffer,clip);
        }
        if(carry.length||!total) throw new Error('Incomplete PCM audio');
      }finally{
        if(!eof) input.cancel().catch(()=>{});
        if(reader===input) reader=null;
        input.releaseLock();
      }
    }

    function pump(){
      if(loading||!current()) return;
      if(!queue.length){
        if(closed&&!clips.size){settled=true;resolveDone(true);}
        return;
      }
      if(clips.size>=2) return;
      const clip={pending:0,downloaded:false};
      clips.add(clip);
      loading=true;
      load(queue.shift(),clip).then(()=>{
        clip.downloaded=true;
        if(!clip.pending) clips.delete(clip);
      }).catch(error=>{
        if(!settled) dispose(error);
      }).finally(()=>{loading=false;pump();});
    }

    return {
      done:done,
      enqueue(text){if(!closed&&current()&&text){queue.push(text);pump();}},
      finish(){closed=true;pump();},
      cancel(){dispose();},
    };
  };
});
