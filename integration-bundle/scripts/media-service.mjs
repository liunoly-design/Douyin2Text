import {readFile,lstat} from 'node:fs/promises';
import {MediaServiceClient,saveMediaResult} from '../src/media-service-client.js';

try {
 const [command,identity,output]=process.argv.slice(2);
 const tokenFile=process.env.MEDIA_SERVICE_TOKEN_FILE;
 if(!tokenFile)throw Error('Set MEDIA_SERVICE_TOKEN_FILE to a private token file');
 const stat=await lstat(tokenFile);
 if(!stat.isFile()||(stat.mode&0o077)||stat.uid!==process.getuid())throw Error('Token file must be an owned regular file with mode 0600');
 const client=new MediaServiceClient({baseUrl:process.env.MEDIA_SERVICE_URL??'http://192.168.31.136:8765',token:(await readFile(tokenFile,'utf8')).trim()});
 let result;
 switch(command){
  case 'health':result=await client.health();break;
  case 'submit':result=await client.submit(identity,{idempotencyKey:process.env.MEDIA_SERVICE_IDEMPOTENCY_KEY});break;
  case 'status':result=await client.status(identity);break;
  case 'fetch':case 'wait':
   if(!output)throw Error('Provide an existing output directory and Markdown file path');
   result=await saveMediaResult(output,command==='wait'?await client.wait(identity):await client.result(identity));break;
  case 'playback':result=await client.playback(identity,output??'video');break;
  case 'probe':result=await client.probe(identity,output??'video');break;
  default:throw Error('Usage: media-service.mjs health|submit URL|status JOB|fetch JOB FILE|wait JOB FILE|playback MEDIA [video|audio]|probe MEDIA [video|audio|cover]');
 }
 console.log(JSON.stringify(result,null,2));
}catch(error){console.error(error.message);process.exitCode=1;}
