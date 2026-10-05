import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {createHash} from 'node:crypto';
import {MediaServiceClient} from '../src/media-service-client.js';

test('replacement ASR engines retain result integrity and required metadata checks',async t=>{
 const markdown='# 原文\n测试文字\n';
 let engine='alternative-local-asr';
 const server=createServer((req,res)=>{
  if(req.url.endsWith('/markdown')){res.setHeader('Content-Type','text/markdown');res.end(markdown);return;}
  res.setHeader('Content-Type','application/json');
  if(!req.url.endsWith('/manifest')){res.end(JSON.stringify({api_version:'1',job_id:'job_1',status:'succeeded'}));return;}
  res.end(JSON.stringify({api_version:'1',job_id:'job_1',source:{platform:'douyin',id:'1234567890123',url:'https://www.douyin.com/video/1234567890123',title:null,author:null,published_at:null,duration_seconds:1},transcription:{engine,model:'fixture-model',model_sha256:'a'.repeat(64),language:'zh'},markdown:{sha256:createHash('sha256').update(markdown).digest('hex')},media:Object.fromEntries(['video','audio','cover'].map(kind=>[kind,{path:'/v1/media/asset/'+kind,sha256:'b'.repeat(64),bytes:16,mime:{video:'video/mp4',audio:'audio/mp4',cover:'image/jpeg'}[kind]}]))}));
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 t.after(()=>new Promise(resolve=>server.close(resolve)));
 const client=new MediaServiceClient({baseUrl:`http://127.0.0.1:${server.address().port}`,token:'fixture-token'});
 assert.equal((await client.result('job_1')).manifest.transcription.engine,'alternative-local-asr');
 for(const invalid of ['',null,42]){
  engine=invalid;
  await assert.rejects(client.result('job_1'),/transcription metadata/);
 }
});
