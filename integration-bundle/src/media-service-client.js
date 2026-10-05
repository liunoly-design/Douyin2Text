import {createHash} from 'node:crypto';
const sha256=value=>createHash('sha256').update(value).digest('hex');
const id=value=>{if(!/^[A-Za-z0-9_-]{1,128}$/.test(value??''))throw Error('Invalid service ID');return value;};
export class MediaServiceClient {
 constructor({baseUrl,token,timeoutMs=15000}) {
  this.base=new URL(baseUrl);
  if(!['http:','https:'].includes(this.base.protocol)||this.base.username||this.base.password||this.base.search||this.base.hash||this.base.pathname!=='/')throw Error('Invalid service base URL');
  if(typeof token!=='string'||!token||/[\r\n]/.test(token))throw Error('Service token required');
  if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw Error('Invalid request timeout');
  this.token=token;this.timeoutMs=timeoutMs;
 }
 async request(path,{method='GET',body,headers={},maxBytes=8*1024*1024}={}) {
  const abort=new AbortController();const timer=setTimeout(()=>abort.abort(),this.timeoutMs);
  try {
   const response=await fetch(new URL(path,this.base),{method,redirect:'error',signal:abort.signal,headers:{...headers,Authorization:`Bearer ${this.token}`,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
   if(!response.ok)throw Error(`Media service HTTP ${response.status}`);
   let size=0;const chunks=[];
   for await(const chunk of response.body){size+=chunk.length;if(size>maxBytes){abort.abort();throw Error('Media service response too large');}chunks.push(chunk);}
   return {response,bytes:Buffer.concat(chunks)};
  }catch(error){
   if(error.message.startsWith('Media service '))throw error;
   throw Error('Media service request interrupted; retain job ID or reuse Idempotency-Key');
  }finally{clearTimeout(timer);}
 }
 async json(path,options) {
  const {response,bytes}=await this.request(path,options);
  if(!response.headers.get('content-type')?.startsWith('application/json'))throw Error('Media service invalid JSON content type');
  let data;try{data=JSON.parse(bytes.toString('utf8'));}catch{throw Error('Media service invalid JSON');}
  if(data?.api_version!=='1')throw Error('Media service incompatible API version');
  return data;
 }
 async health(){const value=await this.json('/health');if(value.status!=='ready')throw Error('Media service not ready');return value;}
 async submit(url,{idempotencyKey}={}) {
  let source;try{source=new URL(url);}catch{throw Error('Invalid Douyin URL');}
  if(source.protocol!=='https:'||source.username||source.password||!['v.douyin.com','douyin.com','www.douyin.com','iesdouyin.com','www.iesdouyin.com'].includes(source.hostname))throw Error('Invalid Douyin URL');
  const body={url:source.href,options:{language:'zh',max_height:1080}};
  const key=idempotencyKey??sha256(JSON.stringify(body));
  if(!/^[A-Za-z0-9_-]{1,128}$/.test(key))throw Error('Invalid Idempotency-Key');
  const data=await this.json('/v1/jobs',{method:'POST',body,headers:{'Idempotency-Key':key}});
  id(data.job_id);return {...data,idempotency_key:key};
 }
 async status(jobId){const data=await this.json(`/v1/jobs/${id(jobId)}`);if(data.job_id!==jobId||!['queued','downloading','extracting','transcribing','succeeded','failed','partial_failed','waiting_login','waiting_confirmation'].includes(data.status))throw Error('Media service invalid task state');return data;}
 async result(jobId) {
  if((await this.status(jobId)).status!=='succeeded')throw Error('Media service result not ready');
  const manifest=await this.json(`/v1/jobs/${id(jobId)}/manifest`);
  if(manifest.job_id!==jobId||!/^[a-f0-9]{64}$/.test(manifest.markdown?.sha256??''))throw Error('Media service invalid manifest');
  const source=manifest.source,asr=manifest.transcription;
  const nullableText=value=>value===null||typeof value==='string';
  if(source?.platform!=='douyin'||!/^\d{10,24}$/.test(source.id??'')||typeof source.url!=='string'||!nullableText(source.title)||!nullableText(source.author)||!(source.published_at===null||typeof source.published_at==='string'&&Number.isFinite(Date.parse(source.published_at)))||!(source.duration_seconds===null||Number.isFinite(source.duration_seconds)&&source.duration_seconds>=0))throw Error('Media service invalid source metadata');
  let sourceURL;try{sourceURL=new URL(source.url);}catch{throw Error('Media service invalid source URL');}
  if(sourceURL.protocol!=='https:'||sourceURL.username||sourceURL.password||!['douyin.com','www.douyin.com','v.douyin.com','iesdouyin.com','www.iesdouyin.com'].includes(sourceURL.hostname))throw Error('Media service invalid source URL');
  if(typeof asr?.engine!=='string'||!asr.engine||typeof asr.model!=='string'||!asr.model||!/^[a-f0-9]{64}$/.test(asr.model_sha256??'')||typeof asr.language!=='string'||!asr.language)throw Error('Media service invalid transcription metadata');
  if(asr.language!=='zh'){
   const tr=manifest.translation;
   if(tr?.kind!=='derived_machine_translation'||tr.source_language!==asr.language||tr.target_language!=='zh'||typeof tr.model!=='string'||!tr.model||!/^[a-f0-9]{64}$/.test(tr.model_sha256??'')||tr.coverage?.all_source_segments_present!==true||!Number.isSafeInteger(tr.coverage.source_segments)||tr.coverage.source_segments<=0)throw Error('Media service Chinese translation missing or incomplete');
  }
  for(const kind of ['video','audio','cover']){
   const media=manifest.media?.[kind];
   if(!new RegExp(`^/v1/media/[A-Za-z0-9_-]{1,128}/${kind}$`).test(media?.path??''))throw Error('Media service invalid media path');
   if(!/^[a-f0-9]{64}$/.test(media.sha256??'')||!Number.isSafeInteger(media.bytes)||media.bytes<=0||typeof media.mime!=='string'||!media.mime.startsWith({video:'video/',audio:'audio/',cover:'image/'}[kind]))throw Error('Media service invalid media metadata');
  }
  const {response,bytes}=await this.request(`/v1/jobs/${id(jobId)}/markdown`);
  if(!response.headers.get('content-type')?.startsWith('text/markdown')||sha256(bytes)!==manifest.markdown.sha256)throw Error('Media service Markdown integrity mismatch');
  let markdown;try{markdown=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);}catch{throw Error('Media service invalid UTF-8');}
  return {manifest,markdown};
 }
 async wait(jobId,{timeoutMs=600000,pollMs=2000}={}) {
  id(jobId);if(!Number.isFinite(timeoutMs)||timeoutMs<=0||!Number.isFinite(pollMs)||pollMs<=0)throw Error('Invalid polling timeout');
  const deadline=Date.now()+timeoutMs;
  while(Date.now()<deadline){const job=await this.status(jobId);if(job.status==='succeeded')return this.result(jobId);if(['failed','partial_failed','waiting_login','waiting_confirmation'].includes(job.status))throw Error(`Media service task ${jobId}: ${job.status}`);await new Promise(r=>setTimeout(r,Math.min(pollMs,Math.max(0,deadline-Date.now()))));}
  throw Error(`Media service wait timed out; resume job ${jobId}`);
 }
 async playback(mediaId,kind='video') {
  if(!['video','audio'].includes(kind))throw Error('Invalid playback kind');
  const data=await this.json(`/v1/media/${id(mediaId)}/playback`,{method:'POST',body:{kind}});
  let url;try{url=new URL(data.url);}catch{throw Error('Media service invalid playback URL');}
  const expires=Date.parse(data.expires_at);
  if(url.origin!==this.base.origin||url.username||url.password||!url.pathname.startsWith('/play/')||!Number.isFinite(expires)||expires<=Date.now()||expires>Date.now()+600000)throw Error('Media service invalid playback URL');
  return data;
 }
 async probe(mediaId,kind='video') {
  if(!['video','audio','cover'].includes(kind))throw Error('Invalid media kind');
  const {response,bytes}=await this.request(`/v1/media/${id(mediaId)}/${kind}`,{headers:{Range:'bytes=0-15'},maxBytes:16});
  const match=/^bytes 0-(\d+)\/(\d+)$/.exec(response.headers.get('content-range')??'');
  if(response.status!==206||!match||Number(match[1])+1!==bytes.length||Number(match[2])<bytes.length)throw Error('Media service Range unsupported');
  return {status:206,bytes:bytes.length,total_bytes:Number(match[2])};
 }
}

export async function saveMediaResult(path,{markdown,manifest}) {
 const {open,lstat,link,unlink}=await import('node:fs/promises');const {constants}=await import('node:fs');const {resolve,dirname,join}=await import('node:path');const {randomUUID}=await import('node:crypto');
 path=resolve(path);
 for(let parent=dirname(path);;parent=dirname(parent)){if(!(await lstat(parent)).isDirectory())throw Error('Result parent must be a regular directory');if(parent===dirname(parent))break;}
 const save=async(file,body)=>{
  const temporary=join(dirname(file),'.media-result-'+randomUUID());let handle;
  try{
   handle=await open(temporary,constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600);
   await handle.writeFile(body);await handle.sync();await handle.close();handle=undefined;
   try{await link(temporary,file);}catch(error){
    if(error.code!=='EEXIST')throw error;
    if(!(await lstat(file)).isFile())throw Error('Result must be a regular file');
    handle=await open(file,constants.O_RDONLY|constants.O_NOFOLLOW);
    if(!(await handle.stat()).isFile())throw Error('Result must be a regular file');
    if(await handle.readFile('utf8')!==body)throw Error('Existing result differs; retain human content');
    await handle.close();handle=undefined;
   }
   handle=await open(dirname(file),constants.O_RDONLY);await handle.sync();
  }finally{await handle?.close();await unlink(temporary).catch(error=>{if(error.code!=='ENOENT')throw error;});}
 };
 await save(path,markdown);await save(path+'.manifest.json',JSON.stringify(manifest,null,2)+'\n');
 return {markdown:path,manifest:path+'.manifest.json'};
}
