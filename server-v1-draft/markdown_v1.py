import json,asyncio,hashlib,time
from pathlib import Path
from datetime import datetime
ROOT=Path('/Users/mac/Documents/Personal-Wiki-Media-Service')
DATA=Path('/Users/mac/Documents/Personal-Wiki-Media')
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def stamp(x):
 ms=round(x*1000);return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02}.{ms%1000:03}'
async def finish(manifest,raw,config,stage):
 vid=manifest['media_id'];translation=None;raw_path=DATA/manifest['files']['raw_transcript']['path'];start=time.monotonic()
 if raw.get('language')=='english' and config.get('translation',{}).get('enabled'):
  stage('transcribing',media_id=vid)
  tr=DATA/'transcripts'/f'{vid}.{config["output_fingerprint"][:12]}.zh.translation.json'
  if tr.exists():
   candidate=json.loads(tr.read_text())
   if candidate.get('source_transcript_sha256')==sha(raw_path) and candidate.get('model_sha256')==config['translation']['sha256'] and candidate.get('quality_revision')==config['translation'].get('quality_revision',2):translation=candidate
  if translation is None:
   proc=await asyncio.create_subprocess_exec(str(ROOT/'.llm-venv/bin/python'),str(ROOT/'translate_mlx.py'),str(raw_path),str(tr),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   try:stdout,stderr=await asyncio.wait_for(proc.communicate(),timeout=1800)
   except BaseException:
    proc.kill();await proc.wait();raise
   if proc.returncode:raise RuntimeError('Local translation failed; original ASR retained')
   translation=json.loads(tr.read_text())
  txt=DATA/'transcripts'/f'{vid}.{config["output_fingerprint"][:12]}.zh.txt';txt.write_text(translation['text'])
  manifest['translation']={k:v for k,v in translation.items() if k not in ('segments','text','raw_model_outputs')}
  for key,path in [('raw_translation',tr),('chinese_text',txt)]:manifest['files'][key]={'path':str(path.relative_to(DATA)),'bytes':path.stat().st_size,'sha256':sha(path)}
  manifest['timings']['translation_seconds']=translation['elapsed_seconds']
 stage('transcribing',media_id=vid)
 m=manifest
 body=f"# {m['title']}\n\n## 来源资料\n\n作者：{m['author']}\n\n发布时间：{m['published_at']}\n\n时长：{m['duration_seconds']:.3f} 秒\n\n平台视频 ID：{vid}\n\n原始分享链接：{m['original_share_url']}\n\n规范来源：{m['canonical_url']}\n\n### 原始描述\n\n{m['description']}\n\n### 本机媒体\n\n在服务首页登录后，用浏览器会话播放；API 使用 Authorization 请求头。也可用客户端 play 命令生成短期播放地址。\n\n封面（平台原始封面）：{m['files']['cover']['url']}\n\n视频：{m['files']['video']['url']}\n\n完整音轨：{m['files']['audio']['url']}\n\n"
 if translation:
  body+='## 完整中文译文（本地机器翻译）\n\n本视频音轨为英文。以下是完整机器译文，继承原始转写的时间范围；不是中文音轨的 ASR，也不是摘要。原文保留在下一节，专有名词和识别误差可据原文复核。\n\n'
  body+='\n\n'.join(f"[{stamp(s['start'])} → {stamp(s['end'])}] {s['text']}" for s in translation['segments'])+'\n\n'
 body+=f"## 完整原始转写（{raw.get('language')}）\n\n"
 body+='\n\n'.join(f"[{stamp(s['start'])} → {stamp(s['end'])}] {s['text']}" for s in raw['segments'])
 body+=f"\n\n## 处理与引用\n\n本机 whisper.cpp，{m['asr']['model']}，自动语种识别，Apple Metal。转写模型 SHA-256：{m['asr']['model_sha256']}。\n\n版本：{json.dumps(m['versions'],ensure_ascii=False)}\n\n转写耗时：{m['asr']['elapsed_seconds']} 秒。处理时间：{m['processed_at']}。\n"
 if translation:body+=f"\n中文翻译：{translation['model']}，{translation['runtime']}，耗时 {translation['elapsed_seconds']} 秒。权重 SHA-256：{translation['model_sha256']}。翻译来源：{translation['model_source']}。\n"
 body+='\n资料引用：上述抖音规范来源、保存的平台原始响应、完整下载的视频、从视频直接封装的完整音轨、保留的 verbose_json 原始转写。未摘要、未删除口头语、未识别说话人。\n'
 md=DATA/'transcripts'/f'{vid}.{config["output_fingerprint"][:12]}.md';md.write_text(body)
 m['files']['markdown']={'path':str(md.relative_to(DATA)),'bytes':md.stat().st_size,'sha256':sha(md)}
 m['output_fingerprint']=config['output_fingerprint'];m['derived_processed_at']=datetime.now().astimezone().isoformat();m['timings']['derived_seconds']=round(time.monotonic()-start,3)
 p=DATA/'metadata'/f'{vid}.{config["asr_fingerprint"][:12]}.manifest.json';temp=p.with_suffix('.json.part');temp.write_text(json.dumps(m,ensure_ascii=False,indent=2));temp.replace(p)
 return m
