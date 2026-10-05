import {readFile,lstat,mkdir,writeFile} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {hostname,networkInterfaces} from 'node:os';
import {createHash} from 'node:crypto';
import {MediaServiceClient,saveMediaResult} from '../src/media-service-client.js';

const output=resolve(process.argv[2]??'lan-results');
const sha=b=>createHash('sha256').update(b).digest('hex');
const report={created_at:new Date().toISOString(),hostname:hostname(),local_ipv4:Object.values(networkInterfaces()).flat().filter(x=>x.family==='IPv4'&&!x.internal).map(x=>x.address),checks:{},acceptance:'待联调'};
try{
 await mkdir(output,{recursive:true,mode:0o700});
 const file=process.env.MEDIA_SERVICE_TOKEN_FILE;
 if(!file)throw Error('Set MEDIA_SERVICE_TOKEN_FILE');
 const stat=await lstat(file);
 if(!stat.isFile()||(stat.mode&0o077)||stat.uid!==process.getuid())throw Error('Token file must be owned regular file with mode 0600');
 const client=new MediaServiceClient({baseUrl:process.env.MEDIA_SERVICE_URL??'http://192.168.31.136:8765',token:(await readFile(file,'utf8')).trim()});
 report.base_url=client.base.origin;
 const health=await client.health();report.observed_client_address=health.client_address;report.checks.health=true;
 report.scope=health.client_address==='192.168.31.78'?'真实调用电脑→Mac mini':'本机检查，不能作为跨设备验收';
 const url='https://v.douyin.com/Tg6ANQWIT7w/';
 const submitted=await client.submit(url);report.job_id=submitted.job_id;report.reused=submitted.reused;
 const repeated=await client.submit(url);
 if(repeated.job_id!==submitted.job_id)throw Error('Idempotency mismatch');report.checks.repeat_submission=true;
 const status=await client.status(submitted.job_id);report.checks.status=true;report.initial_status=status.status;
 const result=await client.wait(submitted.job_id,{timeoutMs:1800000});
 await saveMediaResult(join(output,'transcript.md'),result);report.checks.markdown_and_manifest_saved=true;
 report.markdown_sha256=sha(Buffer.from(result.markdown));report.source_id=result.manifest.source.id;
 report.source_language=result.manifest.transcription.language;report.translation_coverage=result.manifest.translation?.coverage;
 const mediaId=result.manifest.media.video.path.split('/')[3];report.media_id=mediaId;
 for(const kind of ['video','audio','cover'])report.checks[kind+'_range']=await client.probe(mediaId,kind);
 const escapes=s=>s.replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
 const playback={};
 for(const kind of ['video','audio']){
  const p=await client.playback(mediaId,kind);playback[kind]=p.url;
  const r=await fetch(p.url,{redirect:'error',headers:{Range:'bytes=100000-100015'},signal:AbortSignal.timeout(15000)});
  const bytes=Buffer.from(await r.arrayBuffer());
  if(r.status!==206||bytes.length!==16)throw Error('Playback seek Range failed');
  report.checks[kind+'_playback_range']=true;
 }
 await writeFile(join(output,'player.html'),`<!doctype html><meta charset="utf-8"><title>真实局域网播放验收</title><h1>浏览器播放与拖动验收</h1><p>请播放视频及音频，并拖动到约 8 分钟；临时地址最多 10 分钟有效。</p><video id="v" controls preload="metadata" style="max-width:100%;width:900px" src="${escapes(playback.video)}"></video><p><audio id="a" controls src="${escapes(playback.audio)}"></audio></p><pre id="result"></pre><script>for(const k of ['v','a']){const e=document.getElementById(k);for(const ev of ['loadedmetadata','playing','seeked','error'])e.addEventListener(ev,()=>{document.getElementById('result').textContent+=k+' '+ev+' time='+e.currentTime+' duration='+e.duration+'\\n'});}</script>`,{mode:0o600});
 report.player_file=join(output,'player.html');report.browser_seek='待人工或调用电脑 Codex 浏览器确认';
 report.acceptance=health.client_address==='192.168.31.78'?'自动跨设备检查通过，浏览器拖动待确认':'本机自动检查通过，跨设备待联调';
}catch(e){report.error=e.message;process.exitCode=1;}
await mkdir(output,{recursive:true,mode:0o700});
await writeFile(join(output,'lan-report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
console.log(JSON.stringify(report,null,2));
