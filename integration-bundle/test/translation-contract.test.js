import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {createHash} from 'node:crypto';
import {MediaServiceClient} from '../src/media-service-client.js';

test('English ASR requires independently identified complete Chinese translation',async t=>{
 const markdown='# 完整中文译文\n\n[00:00] 中文译文。\n\n# 英文原稿\nOriginal.\n';
 let translation={kind:'derived_machine_translation',source_language:'en',target_language:'zh',model:'local Qwen',model_sha256:'b'.repeat(64),coverage:{source_segments:3,all_source_segments_present:true}};
 const server=createServer((req,res)=>{
  if(req.url.endsWith('/markdown')){res.setHeader('Content-Type','text/markdown; charset=utf-8');res.end(markdown);return;}
  res.setHeader('Content-Type','application/json');
  if(!req.url.endsWith('/manifest')){res.end(JSON.stringify({api_version:'1',job_id:'job_1',status:'succeeded'}));return;}
  res.end(JSON.stringify({api_version:'1',job_id:'job_1',source:{platform:'douyin',id:'7691977131957472558',url:'https://www.douyin.com/video/7691977131957472558',title:null,author:null,published_at:null,duration_seconds:3},transcription:{engine:'whisper.cpp',model:'medium',model_sha256:'a'.repeat(64),language:'en'},translation,markdown:{sha256:createHash('sha256').update(markdown).digest('hex')},media:Object.fromEntries(['video','audio','cover'].map(k=>[k,{path:'/v1/media/m/'+k,sha256:'c'.repeat(64),bytes:30,mime:{video:'video/mp4',audio:'audio/mp4',cover:'image/jpeg'}[k]}]))}));
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>server.close(r)));
 const client=new MediaServiceClient({baseUrl:`http://127.0.0.1:${server.address().port}`,token:'fixture-token'});
 assert.equal((await client.result('job_1')).manifest.transcription.language,'en');
 translation={...translation,coverage:{source_segments:3,all_source_segments_present:false}};
 await assert.rejects(client.result('job_1'),/translation missing or incomplete/);
 translation=undefined;
 await assert.rejects(client.result('job_1'),/translation missing or incomplete/);
});
