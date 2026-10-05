import asyncio,hashlib,json,time,subprocess,os
from pathlib import Path
from safe_network import client as platform_client, video_id
from artifacts import valid_files, safe_path
from datetime import datetime,timezone
import httpx,psutil
from markdown_v1 import finish
from f2.apps.douyin.utils import TokenManager,AwemeIdFetcher,ClientConfManager
from f2.apps.douyin.crawler import DouyinCrawler
from f2.apps.douyin.model import PostDetail
ROOT=Path(__file__).resolve().parent
DATA=Path('/Users/mac/Documents/Personal-Wiki-Media')
CONFIG=json.loads((DATA/'state/config.json').read_text())

def dump(path,value):
    p=Path(path); t=p.with_suffix(p.suffix+'.part');t.write_text(json.dumps(value,ensure_ascii=False,indent=2));t.replace(p)
class LoginRequired(RuntimeError):pass
class ConfirmationRequired(RuntimeError):pass

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def probe(path):
    return json.loads(subprocess.check_output(['/opt/homebrew/bin/ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))
def run(args):
    r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if r.returncode:raise RuntimeError('media command failed: '+r.stderr.decode(errors='replace')[-1500:])
async def fetch(url,path):
    part=path.with_suffix(path.suffix+'.part')
    async with platform_client(media=True,follow_redirects=True,timeout=60,headers=ClientConfManager.headers()) as c:
        async with c.stream('GET',url) as r:
            r.raise_for_status()
            with part.open('wb') as f:
                async for b in r.aiter_bytes():
                    if f.tell()+len(b)>1024**3:raise ConfirmationRequired('Media exceeds 1GB')
                    f.write(b)
    if not part.stat().st_size:raise RuntimeError('empty download')
    part.replace(path)
async def fetch_candidates(urls,path):
    for url in urls:
        try:await fetch(url,path);return
        except ConfirmationRequired:raise
        except Exception:pass
    raise RuntimeError('All platform media URLs failed; resubmit to refresh metadata')
def record(path,kind):
    return {'path':str(path.relative_to(DATA)),'bytes':path.stat().st_size,'sha256':digest(path),'url':CONFIG['base_url']+'/v1/media/'+path.stem+'/'+kind}
async def process(job,stage):
    start=time.monotonic();timings={}
    stage('downloading')
    vid=await video_id(job['url'],ClientConfManager.headers())
    if not vid.isdigit():raise RuntimeError('Invalid platform video ID')
    stage('downloading',media_id=vid)
    manifest_path=DATA/'metadata'/f'{vid}.{CONFIG["asr_fingerprint"][:12]}.manifest.json'
    legacy_path=DATA/'metadata'/f'{vid}.manifest.json'
    if not manifest_path.exists() and legacy_path.exists():
        prior=json.loads(legacy_path.read_text())
        if prior.get('asr',{}).get('fingerprint')==CONFIG['asr_fingerprint'] and valid_files(prior):
            dump(manifest_path,prior)
    fingerprint=CONFIG['asr_fingerprint']
    if manifest_path.exists():
        previous=json.loads(manifest_path.read_text())
        if previous.get('asr',{}).get('fingerprint')==fingerprint and valid_files(previous):
            if previous.get('output_fingerprint')==CONFIG.get('output_fingerprint'):
                stage('transcribing',media_id=vid,reused=True);return previous
            raw=json.loads(safe_path(previous['files']['raw_transcript']['path']).read_text())
            return await finish(previous,raw,CONFIG,stage)
    raw_path=DATA/'metadata'/f'{vid}.{job["id"]}.raw.json'
    # Fetch fresh visitor metadata when no valid final cache exists. No browser-cookie extraction.
    cookie='ttwid='+await asyncio.to_thread(TokenManager.gen_ttwid)+';'
    async with DouyinCrawler({'cookie':cookie,'headers':ClientConfManager.headers(),'timeout':30,'max_retries':2}) as c:
        c._aclient=platform_client(headers=c.crawler_headers,timeout=30,follow_redirects=True)
        raw=await c.fetch_post_detail(PostDetail(aweme_id=vid))
    dump(raw_path,raw)
    a=raw.get('aweme_detail')
    if not a or not a.get('author'):raise LoginRequired('Public visitor metadata unavailable')
    v=a['video']
    if v.get('duration',0)>1800000:raise ConfirmationRequired('Video exceeds 30 minutes')
    candidates=[]
    for b in v.get('bit_rate',[]):
        addr=b.get('play_addr',{}); w=addr.get('width',v.get('width',0));h=addr.get('height',v.get('height',0))
        if w and h and min(w,h)<=1080 and addr.get('url_list'):candidates.append((min(w,h),b.get('bit_rate',0),b))
    if not candidates:raise RuntimeError('No verified video candidate at or below 1080p')
    selected=max(candidates,key=lambda x:x[:2])[2]
    video=DATA/'video'/f'{vid}.mp4'; cover=DATA/'metadata'/f'{vid}.cover'
    selection_path=DATA/'metadata'/f'{vid}.selection.json'
    selection={'gear':selected.get('gear_name'),'bit_rate':selected.get('bit_rate'),'address':selected['play_addr']}
    if video.exists():safe_path(str(video.relative_to(DATA)))
    if not video.exists():
        if selected['play_addr'].get('data_size',0)>1024**3:raise ConfirmationRequired('Video exceeds 1GB')
        await fetch_candidates(selected['play_addr']['url_list'],video);selection['sha256']=digest(video);dump(selection_path,selection)
    elif selection_path.exists():
        selection=json.loads(selection_path.read_text())
        if selection.get('sha256') and digest(video)!=selection['sha256']:raise RuntimeError('Stored video integrity mismatch')
    else:raise RuntimeError('Existing video lacks provenance; manual inspection required')
    if cover.exists():safe_path(str(cover.relative_to(DATA)))
    if not cover.exists():await fetch_candidates((v.get('origin_cover') or v['cover'])['url_list'],cover)
    vp=await asyncio.to_thread(probe,video)
    vs=next(s for s in vp['streams'] if s['codec_type']=='video')
    if min(vs['width'],vs['height'])>1080:raise RuntimeError('Downloaded video exceeds 1080p policy')
    timings['download_seconds']=round(time.monotonic()-start,3)
    stage('extracting',media_id=vid)
    t=time.monotonic();audio=DATA/'audio'/f'{vid}.m4a'
    ac=next(s for s in vp['streams'] if s['codec_type']=='audio')['codec_name']
    method='copy original audio stream'
    if ac not in ('aac','alac'):
        audio=DATA/'audio'/f'{vid}.mka'
    if audio.exists():safe_path(str(audio.relative_to(DATA)))
    if not audio.exists():
        tmp=audio.with_name(vid+'.partial'+audio.suffix)
        await asyncio.to_thread(run,['/opt/homebrew/bin/ffmpeg','-nostdin','-y','-v','error','-i',str(video),'-map','0:a:0','-vn','-c:a','copy',str(tmp)])
        tmp.replace(audio)
    wav=DATA/'tmp'/f'{vid}.asr.wav'
    await asyncio.to_thread(run,['/opt/homebrew/bin/ffmpeg','-nostdin','-y','-v','error','-i',str(audio),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(wav)])
    ap=await asyncio.to_thread(probe,audio)
    # Decode the complete video and audio, not merely inspect headers.
    for path in (video,audio):await asyncio.to_thread(run,['/opt/homebrew/bin/ffmpeg','-nostdin','-v','error','-xerror','-i',str(path),'-f','null','-'])
    duration=float(vp['format']['duration']);aduration=float(ap['format']['duration'])
    if duration>1800 or video.stat().st_size>1024**3:raise ConfirmationRequired('Media exceeds configured limits')
    if abs(duration-aduration)>0.25:raise RuntimeError('Video/audio duration mismatch')
    timings['extract_and_validate_seconds']=round(time.monotonic()-t,3)
    stage('transcribing',media_id=vid)
    # launchd may start both processes together; await native model readiness.
    async with httpx.AsyncClient(timeout=2,trust_env=False) as readiness:
        for attempt in range(300):
            try:
                ready=await readiness.get('http://127.0.0.1:8767/health')
                if ready.status_code==200:break
            except httpx.HTTPError:pass
            await asyncio.sleep(2)
        else:raise RuntimeError('Local whisper-server not ready after 10 minutes')
    asr_raw=DATA/'transcripts'/f'{vid}.{fingerprint[:12]}.raw.json';t=time.monotonic()
    peak=0;stop=asyncio.Event()
    async def monitor():
        nonlocal peak
        while not stop.is_set():
            for p in psutil.process_iter(['name','memory_info']):
                if p.info['name']=='whisper-server':peak=max(peak,p.info['memory_info'].rss)
            try:await asyncio.wait_for(stop.wait(),timeout=.5)
            except asyncio.TimeoutError:pass
    watcher=asyncio.create_task(monitor())
    asr_checkpoint=asr_raw.with_suffix('.checkpoint.json')
    video_hash=await asyncio.to_thread(digest,video)
    reusable=None
    if asr_raw.exists() and asr_checkpoint.exists():
        checkpoint=json.loads(asr_checkpoint.read_text())
        if checkpoint.get('video_sha256')==video_hash and checkpoint.get('fingerprint')==fingerprint and checkpoint.get('raw_sha256')==digest(asr_raw):
            reusable=json.loads(asr_raw.read_text())
    try:
        if reusable is not None:
            result=reusable
        else:
            async with httpx.AsyncClient(timeout=7200,trust_env=False) as c:
                with wav.open('rb') as f:
                    r=await c.post('http://127.0.0.1:8767/inference',files={'file':(wav.name,f,'audio/wav')},data={'response_format':'verbose_json','language':'auto','temperature':'0.0'})
                r.raise_for_status();result=r.json();dump(asr_raw,result)
            dump(asr_checkpoint,{'video_sha256':video_hash,'fingerprint':fingerprint,'raw_sha256':digest(asr_raw)})
    finally:stop.set();await watcher
    timings['transcribe_seconds']=round(time.monotonic()-t,3)
    segments=result.get('segments',[])
    if not segments or not result.get('text'):raise RuntimeError('ASR returned no transcription')
    if max(s['end'] for s in segments)<duration-10:raise RuntimeError('ASR ends too early; raw output retained for inspection')
    from collections import Counter
    repeated=Counter(s['text'].strip() for s in segments if s['text'].strip())
    if repeated and max(repeated.values())>max(20,len(segments)*.3):raise RuntimeError('ASR content QA failed: excessive repeated segments; original result retained')
    full=DATA/'transcripts'/f'{vid}.{fingerprint[:12]}.txt';full.write_text(result['text'])
    model=CONFIG['model']
    manifest={'media_id':vid,'platform':'douyin','original_share_url':job['url'],'canonical_url':f'https://www.douyin.com/video/{vid}',
      'title':a.get('item_title') or a.get('preview_title') or a['desc'].split('\n')[0], 'description':a['desc'],'author':a['author']['nickname'],
      'published_at':datetime.fromtimestamp(a['create_time'],timezone.utc).astimezone().isoformat(),'duration_seconds':duration,'platform_duration_seconds':v['duration']/1000,
      'selected_video':{'width':vs['width'],'height':vs['height'],'codec':vs['codec_name'],'gear':selection['gear'],'bit_rate':selection['bit_rate']},
      'audio':{'method':method,'codec':ac,'duration_seconds':aduration,'asr_format':'PCM signed 16-bit WAV, 16000Hz mono'},
      'cover_kind':'platform original cover','versions':CONFIG['versions'],
      'asr':{'model':model['name'],'model_source':model['source'],'model_sha256':model['sha256'],'fingerprint':fingerprint,'language':result.get('language'),'parameters':{'temperature':0.0,'threads':4,'best_of':2},'elapsed_seconds':timings['transcribe_seconds'],'peak_server_rss_bytes':peak,'note':'RSS sampled every 0.5s; Metal unified allocations not fully represented'},
      'timings':timings,'processed_at':datetime.now().astimezone().isoformat(),'validation':{'full_video_decode':True,'full_audio_decode':True,'duration_difference_seconds':round(abs(duration-aduration),6),'last_segment_end':max(s['end'] for s in segments)},
      'files':{}}
    for key,path in [('video',video),('audio',audio),('cover',cover),('raw_metadata',raw_path),('raw_transcript',asr_raw),('text',full)]:
        manifest['files'][key]={'path':str(path.relative_to(DATA)),'bytes':path.stat().st_size,'sha256':digest(path)}
        if key in ('video','audio','cover'):manifest['files'][key]['url']=CONFIG['base_url']+f'/v1/media/{vid}/{key}'
    timings['total_seconds']=round(time.monotonic()-start,3)
    final=await finish(manifest,result,CONFIG,stage)
    wav.unlink(missing_ok=True)
    return final
