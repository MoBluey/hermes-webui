/* Pure cumulative-stream to speakable-sentence tracker. */
(function(root,factory){
  const create=factory();
  if(typeof module==='object'&&module.exports) module.exports=create;
  if(root) root.createHermesVoiceSentenceTracker=create;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function visibleProse(value){
    const text=String(value||'');
    let out='';
    let cursor=0;
    let fence='';
    while(cursor<text.length){
      const triple=text.slice(cursor,cursor+3);
      if(triple==='```'||triple==='~~~'){
        if(!fence) fence=triple;
        else if(fence===triple){ fence=''; out+=' '; }
        cursor+=3;
        continue;
      }
      if(fence){ cursor++; continue; }
      if(text[cursor]==='`'){
        const width=text[cursor+1]==='`'?2:1;
        const delimiter='`'.repeat(width);
        const close=text.indexOf(delimiter,cursor+width);
        if(close<0) break;
        out+=' ';
        cursor=close+width;
        continue;
      }
      out+=text[cursor++];
    }
    out=out.replace(/(^|\s)\d+[.)]\s+/g,'$1');
    return out.replace(/\s+/g,' ').trimStart();
  }

  return function createHermesVoiceSentenceTracker(){
    let snapshot='';
    let emittedSlots=0;

    function update(value,done){
      snapshot=visibleProse(value);
      const result=[];
      const sentences=[];
      let rest=snapshot;
      const pattern=/^([\s\S]*?[.!?](?:["')\]]+)?)(?=\s|$)/;
      while(rest){
        const match=rest.match(pattern);
        if(!match) break;
        const sentence=match[1].trim();
        rest=rest.slice(match[1].length).trimStart();
        if(sentence) sentences.push(sentence);
      }
      if(done){
        const tail=rest.trim();
        if(tail) sentences.push(tail);
      }
      for(let index=emittedSlots;index<sentences.length;index++) result.push(sentences[index]);
      emittedSlots=Math.max(emittedSlots,sentences.length);
      return result;
    }

    return {update:update,reset:function(){snapshot='';emittedSlots=0;}};
  };
});
