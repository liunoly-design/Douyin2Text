import asyncio,hashlib,json,time,subprocess,os
from pathlib import Path
from .safe_network import client as platform_client
from .artifacts import valid_files, safe_path
from datetime import datetime,timezone
from .markdown import finish
from .providers.contracts import LoginRequired, ConfirmationRequired
from .providers.registry import source_provider, asr_provider
from .runtime import DATA, load_config
ROOT=Path(__file__).resolve().parent
CONFIG=load_config()

def dump(path,value):
    p=Path(path); t=p.with_suffix(p.suffix+'.part');t.write_text(json.dumps(value,ensure_ascii=False,indent=2));t.replace(p)

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
async def fetch(url,path,headers):
    part=path.with_suffix(path.suffix+'.part')
    async with platform_client(media=True,follow_redirects=True,timeout=60,headers=headers) as c:
        async with c.stream('GET',url) as r:
            r.raise_for_status()
            with part.open('wb') as f:
                async for b in r.aiter_bytes():
                    if f.tell()+len(b)>1024**3:raise ConfirmationRequired('Media exceeds 1GB')
                    f.write(b)
    if not part.stat().st_size:raise RuntimeError('empty download')
    part.replace(path)
async def fetch_candidates(urls,path,headers):
    for url in urls:
        try:await fetch(url,path,headers);return
        except ConfirmationRequired:raise
        except Exception:pass
    raise RuntimeError('All platform media URLs failed; resubmit to refresh metadata')
def record(path,kind):
    return {'path':str(path.relative_to(DATA)),'bytes':path.stat().st_size,'sha256':digest(path),'url':CONFIG['base_url']+'/v1/media/'+path.stem+'/'+kind}
async def process(job,stage,*,source=None,transcriber=None):
    source = source or source_provider(CONFIG)
    transcriber = transcriber or asr_provider(CONFIG)
    start=time.monotonic();timings={}
    stage('downloading')
    vid=await source.resolve_id(job['url'])
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
    info = await source.describe(vid, max_short_edge=1080)
    if info.media_id != vid:raise RuntimeError('Source provider returned mismatched media ID')
    if info.duration_seconds > 1800:raise ConfirmationRequired('Video exceeds 30 minutes')
    dump(raw_path,dict(info.raw_metadata))
    video=DATA/'video'/f'{vid}.mp4'; cover=DATA/'metadata'/f'{vid}.cover'
    selection_path=DATA/'metadata'/f'{vid}.selection.json'
    selection=dict(info.selection)
    if video.exists():safe_path(str(video.relative_to(DATA)))
    if not video.exists():
        if info.expected_bytes>1024**3:raise ConfirmationRequired('Video exceeds 1GB')
        await fetch_candidates(info.video_urls,video,source.headers());selection['sha256']=digest(video);dump(selection_path,selection)
    elif selection_path.exists():
        selection=json.loads(selection_path.read_text())
        if selection.get('sha256') and digest(video)!=selection['sha256']:raise RuntimeError('Stored video integrity mismatch')
    else:raise RuntimeError('Existing video lacks provenance; manual inspection required')
    if cover.exists():safe_path(str(cover.relative_to(DATA)))
    if not cover.exists():await fetch_candidates(info.cover_urls,cover,source.headers())
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
    asr_raw=DATA/'transcripts'/f'{vid}.{fingerprint[:12]}.raw.json';t=time.monotonic()
    peak=0
    asr_checkpoint=asr_raw.with_suffix('.checkpoint.json')
    video_hash=await asyncio.to_thread(digest,video)
    reusable=None
    if asr_raw.exists() and asr_checkpoint.exists():
        checkpoint=json.loads(asr_checkpoint.read_text())
        if checkpoint.get('video_sha256')==video_hash and checkpoint.get('fingerprint')==fingerprint and checkpoint.get('raw_sha256')==digest(asr_raw):
            reusable=json.loads(asr_raw.read_text())
    if reusable is not None:
        result=reusable
    else:
        transcript=await transcriber.transcribe(wav)
        result=transcript.as_dict();peak=transcript.peak_rss_bytes
        dump(asr_raw,result)
        dump(asr_checkpoint,{'video_sha256':video_hash,'fingerprint':fingerprint,'raw_sha256':digest(asr_raw)})
    timings['transcribe_seconds']=round(time.monotonic()-t,3)
    segments=result.get('segments',[])
    if not segments or not result.get('text'):raise RuntimeError('ASR returned no transcription')
    if max(s['end'] for s in segments)<duration-10:raise RuntimeError('ASR ends too early; raw output retained for inspection')
    from collections import Counter
    repeated=Counter(s['text'].strip() for s in segments if s['text'].strip())
    if repeated and max(repeated.values())>max(20,len(segments)*.3):raise RuntimeError('ASR content QA failed: excessive repeated segments; original result retained')
    full=DATA/'transcripts'/f'{vid}.{fingerprint[:12]}.txt';full.write_text(result['text'])
    model=CONFIG['model']
    manifest={'media_id':vid,'platform':'douyin','original_share_url':job['url'],'canonical_url':info.canonical_url,
      'title':info.title, 'description':info.description,'author':info.author,
      'published_at':info.published_at,'duration_seconds':duration,'platform_duration_seconds':info.duration_seconds,
      'selected_video':{'width':vs['width'],'height':vs['height'],'codec':vs['codec_name'],'gear':selection.get('gear'),'bit_rate':selection.get('bit_rate')},
      'audio':{'method':method,'codec':ac,'duration_seconds':aduration,'asr_format':'PCM signed 16-bit WAV, 16000Hz mono'},
      'cover_kind':'platform original cover','versions':CONFIG['versions'],
      'source_provider':source.name,
      'asr':{'engine':transcriber.name,'model':model['name'],'model_source':model['source'],'model_sha256':model['sha256'],'fingerprint':fingerprint,'language':result.get('language'),'parameters':dict(transcriber.parameters),'elapsed_seconds':timings['transcribe_seconds'],'peak_server_rss_bytes':peak,'note':'Provider-specific RSS measurement; accelerator allocations may not be included'},
      'timings':timings,'processed_at':datetime.now().astimezone().isoformat(),'validation':{'full_video_decode':True,'full_audio_decode':True,'duration_difference_seconds':round(abs(duration-aduration),6),'last_segment_end':max(s['end'] for s in segments)},
      'files':{}}
    for key,path in [('video',video),('audio',audio),('cover',cover),('raw_metadata',raw_path),('raw_transcript',asr_raw),('text',full)]:
        manifest['files'][key]={'path':str(path.relative_to(DATA)),'bytes':path.stat().st_size,'sha256':digest(path)}
        if key in ('video','audio','cover'):manifest['files'][key]['url']=CONFIG['base_url']+f'/v1/media/{vid}/{key}'
    timings['total_seconds']=round(time.monotonic()-start,3)
    final=await finish(manifest,result,CONFIG,stage)
    wav.unlink(missing_ok=True)
    return final
