import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {MediaServiceClient} from '../src/media-service-client.js';

async function serve(t, handler) {
 const server=createServer(handler);await new Promise(r=>server.listen(0,'127.0.0.1',r));
 t.after(()=>new Promise(r=>server.close(r)));
 return `http://127.0.0.1:${server.address().port}`;
}
function completeManifest(value) {
 return {...value,source:{platform:'douyin',id:'7691977131957472558',url:'https://www.douyin.com/video/7691977131957472558',title:'合成标题',author:'合成作者',published_at:null,duration_seconds:10},transcription:{engine:'whisper.cpp',model:'small',model_sha256:'a'.repeat(64),language:'zh'},media:Object.fromEntries(Object.entries(value.media).map(([kind,media])=>[kind,{...media,sha256:'b'.repeat(64),bytes:1000,mime:{video:'video/mp4',audio:'audio/mp4',cover:'image/jpeg'}[kind]}]))};
}
test('submit uses authenticated versioned HTTP and stable retry identity',async t=>{
 const keys=[];
 const url=await serve(t,async(req,res)=>{
  assert.equal(req.url,'/v1/jobs');assert.equal(req.headers.authorization,'Bearer fixture-token');
  let data='';for await(const chunk of req)data+=chunk;
  assert.equal(JSON.parse(data).url,'https://v.douyin.com/example/');keys.push(req.headers['idempotency-key']);
  res.writeHead(202,{'Content-Type':'application/json'});res.end(JSON.stringify({api_version:'1',job_id:'job_1',status:'queued'}));
 });
 const client=new MediaServiceClient({baseUrl:url,token:'fixture-token'});
 assert.equal((await client.submit('https://v.douyin.com/example/')).job_id,'job_1');
 await client.submit('https://v.douyin.com/example/');assert.equal(keys[0],keys[1]);assert.equal(keys[0].length,64);
});

test('complete HTTP workflow returns verified Markdown, remote media and seek probe',async t=>{
 const {createHash}=await import('node:crypto');const markdown='# 合成视频\n\n[00:00] 完整中文机器稿。\n';
 const digest=createHash('sha256').update(markdown).digest('hex');let origin;
 origin=await serve(t,(req,res)=>{
  if(req.url.endsWith('/video')){assert.equal(req.headers.range,'bytes=0-15');res.writeHead(206,{'Content-Range':'bytes 0-15/1000'});res.end('0123456789abcdef');return;}
  if(req.url.endsWith('/markdown')){res.writeHead(200,{'Content-Type':'text/markdown; charset=utf-8'});res.end(markdown);return;}
  res.setHeader('Content-Type','application/json');
  let value={api_version:'1',job_id:'job_1',status:'succeeded'};
  if(req.url.endsWith('/manifest'))value={...value,markdown:{sha256:digest},media:{video:{path:'/v1/media/media_1/video'},audio:{path:'/v1/media/media_1/audio'},cover:{path:'/v1/media/media_1/cover'}}};
  if(req.url.endsWith('/playback'))value={api_version:'1',url:origin+'/play/short-lived',expires_at:new Date(Date.now()+60000).toISOString()};
  res.end(JSON.stringify(value.media?completeManifest(value):value));
 });
 const client=new MediaServiceClient({baseUrl:origin,token:'fixture-token'});
 assert.equal((await client.wait('job_1')).markdown,markdown);
 assert.equal((await client.playback('media_1')).url,origin+'/play/short-lived');
 assert.deepEqual(await client.probe('media_1'),{status:206,bytes:16,total_bytes:1000});
});

test('authentication failure never reveals server message or token',async t=>{
 const origin=await serve(t,(req,res)=>{res.writeHead(401);res.end('fixture-token secret error');});
 const client=new MediaServiceClient({baseUrl:origin,token:'fixture-token'});
 await assert.rejects(client.health(),{message:'Media service HTTP 401'});
});

test('does not follow redirects with credentials',async t=>{
 let leaked=false;
 const target=await serve(t,(_req,res)=>{leaked=true;res.end('secret');});
 const origin=await serve(t,(_req,res)=>{res.writeHead(302,{Location:target});res.end();});
 await assert.rejects(new MediaServiceClient({baseUrl:origin,token:'fixture-token'}).health(),/interrupted/);
 assert.equal(leaked,false);
});

test('waiting ends on recoverable terminal states and preserves job identity on timeout',async t=>{
 let status='waiting_login';
 const origin=await serve(t,(_req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({api_version:'1',job_id:'job_1',status}));});
 const client=new MediaServiceClient({baseUrl:origin,token:'fixture-token'});
 await assert.rejects(client.wait('job_1'),/job_1: waiting_login/);
 status='queued';await assert.rejects(client.wait('job_1',{timeoutMs:20,pollMs:5}),/resume job job_1/);
});

test('corrupt or foreign results are rejected before writing',async t=>{
 let mode='hash';
 const origin=await serve(t,(req,res)=>{
  if(req.url.endsWith('/markdown')){res.setHeader('Content-Type','text/markdown');res.end('tampered');return;}
  res.setHeader('Content-Type','application/json');
  const data={api_version:'1',job_id:'job_1',status:'succeeded'};
  if(req.url.endsWith('/manifest'))Object.assign(data,{markdown:{sha256:'0'.repeat(64)},media:{video:{path:mode==='path'?'https://foreign.example/video':'/v1/media/m/video'},audio:{path:'/v1/media/m/audio'},cover:{path:'/v1/media/m/cover'}}});
  res.end(JSON.stringify(data.media?completeManifest(data):data));
 });
 const client=new MediaServiceClient({baseUrl:origin,token:'fixture-token'});
 await assert.rejects(client.result('job_1'),/integrity mismatch/);
 mode='path';await assert.rejects(client.result('job_1'),/invalid media path/);
});

test('request timeout, incompatible protocol and absent Range do not become success',async t=>{
 let mode='timeout';
 const origin=await serve(t,(_req,res)=>{
  if(mode==='timeout')return;
  if(mode==='version'){res.setHeader('Content-Type','application/json');res.end('{"api_version":"2","status":"ready"}');return;}
  res.end('0123456789abcdef');
 });
 const client=new MediaServiceClient({baseUrl:origin,token:'fixture-token',timeoutMs:50});
 await assert.rejects(client.health(),/interrupted/);
 mode='version';await assert.rejects(client.health(),/incompatible API version/);
 mode='range';await assert.rejects(client.probe('media_1'),/Range unsupported/);
});

test('saving results is repeatable and protects existing edits and symlinks',async t=>{
 const {mkdtemp,realpath,readFile,writeFile,symlink,rm}=await import('node:fs/promises');
 const {tmpdir}=await import('node:os');const {join}=await import('node:path');
 const {saveMediaResult}=await import('../src/media-service-client.js');
 const root=await realpath(await mkdtemp(join(tmpdir(),'wiki-media-')));t.after(()=>rm(root,{recursive:true,force:true}));
 const path=join(root,'transcript.md');const result={markdown:'# 机器稿\n',manifest:{job_id:'job_1'}};
 await saveMediaResult(path,result);await saveMediaResult(path,result);
 assert.equal(await readFile(path,'utf8'),'# 机器稿\n');
 await writeFile(path,'人工校订');await assert.rejects(saveMediaResult(path,result),/Existing result differs/);
 assert.equal(await readFile(path,'utf8'),'人工校订');
 const alias=join(root,'alias.md');await symlink(path,alias);await assert.rejects(saveMediaResult(alias,result),/regular file/);
});

test('CLI retrieves Markdown and manifest using a private token file without fetching videos',async t=>{
 const {mkdtemp,realpath,writeFile,readFile,rm}=await import('node:fs/promises');
 const {tmpdir}=await import('node:os');const {join}=await import('node:path');const {spawn}=await import('node:child_process');const {createHash}=await import('node:crypto');
 const root=await realpath(await mkdtemp(join(tmpdir(),'wiki-media-cli-')));t.after(()=>rm(root,{recursive:true,force:true}));
 const tokenFile=join(root,'token');await writeFile(tokenFile,'fixture-token',{mode:0o600});
 const markdown='# CLI合成稿\n';const digest=createHash('sha256').update(markdown).digest('hex');
 const manifest=completeManifest({api_version:'1',job_id:'job_1',markdown:{sha256:digest},media:{video:{path:'/v1/media/m/video'},audio:{path:'/v1/media/m/audio'},cover:{path:'/v1/media/m/cover'}}});
 const origin=await serve(t,(req,res)=>{
  assert.equal(req.headers.authorization,'Bearer fixture-token');
  assert.ok(!req.url.includes('/v1/media/'),'CLI must leave video/audio on server');
  if(req.url.endsWith('/markdown')){res.setHeader('Content-Type','text/markdown');res.end(markdown);return;}
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify(req.url.endsWith('/manifest')?manifest:{api_version:'1',job_id:'job_1',status:'succeeded'}));
 });
 const output=join(root,'transcript.md');
 const run=async()=>new Promise((accept,reject)=>{const child=spawn(process.execPath,['scripts/media-service.mjs','fetch','job_1',output],{env:{...process.env,MEDIA_SERVICE_URL:origin,MEDIA_SERVICE_TOKEN_FILE:tokenFile}});let stdout='',stderr='';child.stdout.on('data',v=>stdout+=v);child.stderr.on('data',v=>stderr+=v);child.on('error',reject);child.on('close',code=>accept({code,stdout,stderr}));});
 const first=await run();assert.equal(first.code,0,first.stderr);assert.ok(!first.stdout.includes('fixture-token'));
 assert.equal(await readFile(output,'utf8'),markdown);assert.deepEqual(JSON.parse(await readFile(output+'.manifest.json','utf8')),manifest);
 await writeFile(output,'用户修改');const second=await run();assert.equal(second.code,1);assert.equal(await readFile(output,'utf8'),'用户修改');
});

test('concurrent identical saves publish complete files without conflicts',async t=>{
 const {mkdtemp,realpath,readFile,rm}=await import('node:fs/promises');const {tmpdir}=await import('node:os');const {join}=await import('node:path');
 const {saveMediaResult}=await import('../src/media-service-client.js');
 const root=await realpath(await mkdtemp(join(tmpdir(),'wiki-media-concurrent-')));t.after(()=>rm(root,{recursive:true,force:true}));
 const path=join(root,'result.md');const result={markdown:'中文正文\n'.repeat(10000),manifest:{job_id:'job_1'}};
 await Promise.all(Array.from({length:10},()=>saveMediaResult(path,result)));
 assert.equal(await readFile(path,'utf8'),result.markdown);
});

test('missing mandatory source metadata is rejected despite a valid Markdown hash',async t=>{
 const origin=await serve(t,(req,res)=>{
  res.setHeader('Content-Type','application/json');
  res.end(JSON.stringify(req.url.endsWith('/manifest')?{api_version:'1',job_id:'job_1',markdown:{sha256:'a'.repeat(64)},media:{video:{path:'/v1/media/m/video'},audio:{path:'/v1/media/m/audio'},cover:{path:'/v1/media/m/cover'}}}:{api_version:'1',job_id:'job_1',status:'succeeded'}));
 });
 await assert.rejects(new MediaServiceClient({baseUrl:origin,token:'fixture-token'}).result('job_1'),/invalid source metadata/);
});

test('retry completes a missing manifest while ignoring an interrupted temporary file',async t=>{
 const {mkdtemp,realpath,writeFile,readFile,rm}=await import('node:fs/promises');const {tmpdir}=await import('node:os');const {join}=await import('node:path');
 const {saveMediaResult}=await import('../src/media-service-client.js');
 const root=await realpath(await mkdtemp(join(tmpdir(),'wiki-media-recovery-')));t.after(()=>rm(root,{recursive:true,force:true}));
 const path=join(root,'result.md');await writeFile(path,'# 完整正文\n');await writeFile(join(root,'.media-result-crashed'),'半写入');
 await saveMediaResult(path,{markdown:'# 完整正文\n',manifest:{job_id:'job_1'}});
 assert.equal(JSON.parse(await readFile(path+'.manifest.json','utf8')).job_id,'job_1');
 assert.equal(await readFile(path,'utf8'),'# 完整正文\n');
});
