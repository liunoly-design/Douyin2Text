# 调用电脑真实局域网验收

验收状态：待联调。服务地址 `http://192.168.31.136:8765`。以下操作由 192.168.31.78 本机的 Codex 执行，不需要远程 SSH 执行命令。Node >=24。

## 安全取得令牌

长期令牌仍是 Mac mini 的 `/Users/mac/Documents/Personal-Wiki-Media/state/api-token`（0600），本次没有更换。优先复用调用电脑已配置的令牌文件；禁止打印内容或放入聊天、命令参数、URL、日志。

如尚未配置，可以在 Mac mini 用 Finder/AirDrop 把该文件发送给自己的调用电脑，在调用电脑移入 `~/.personal-wiki-media/token` 并 `chmod 600`。也可用下面的 SCP 仅加密复制凭据（不会在调用电脑开放 SSH，不使用 SSH 远程执行开发或测试命令）：

```sh
mkdir -p "$HOME/.personal-wiki-media"
chmod 700 "$HOME/.personal-wiki-media"
scp mac@192.168.31.136:/Users/mac/Documents/Personal-Wiki-Media/state/api-token "$HOME/.personal-wiki-media/token"
chmod 600 "$HOME/.personal-wiki-media/token"
```

首次 SCP 时核对 mini 的 ED25519 主机公钥指纹：`SHA256:H8iiYqGs1M5u6Y1g05HmmvKTQGFhRzyx2P54Ni52+i8`。密码只由用户在调用电脑终端输入。若已有同路径令牌，不重复覆盖。

## 下载已同步的契约和客户端

原客户端只接受中文 ASR；本真实样本为英文音轨加中文译文。必须使用本包新版客户端，不能把原英文 ASR 标成中文来绕过检查。此包的 `docs/contracts/media-service-v1.md` 已同步说明变化。

```sh
export MEDIA_SERVICE_URL='http://192.168.31.136:8765'
export MEDIA_SERVICE_TOKEN_FILE="$HOME/.personal-wiki-media/token"
MEDIA_LAN_DIR="$HOME/.personal-wiki-media/v1-lan-20261005"
mkdir -p "$MEDIA_LAN_DIR"
chmod 700 "$MEDIA_LAN_DIR"
cd "$MEDIA_LAN_DIR"
node --input-type=module <<'JS'
import {readFile,writeFile,lstat} from 'node:fs/promises';
const p=process.env.MEDIA_SERVICE_TOKEN_FILE;
const s=await lstat(p);
if(!s.isFile()||(s.mode&0o077)||s.uid!==process.getuid())throw Error('Token file must be owned regular file with mode 0600');
const token=(await readFile(p,'utf8')).trim();
const r=await fetch(process.env.MEDIA_SERVICE_URL+'/v1/client-bundle',{redirect:'error',headers:{Authorization:'Bearer '+token},signal:AbortSignal.timeout(15000)});
if(!r.ok)throw Error('Client bundle HTTP '+r.status);
await writeFile('client.zip',Buffer.from(await r.arrayBuffer()),{mode:0o600});
console.log('Client bundle downloaded; no credentials printed');
JS
unzip -n client.zip -d client
cd client
node --test test/*.test.js
```

使用独立目录，不覆盖原调用电脑仓库或 Vault。目录已存在时先确认其内容；若服务端升级生成新包，用新的目录解压，避免 `unzip -n` 保留旧脚本。

## 一次运行全部自动联调

```sh
node scripts/lan-acceptance.mjs "$MEDIA_LAN_DIR/results"
```

脚本真实执行 health、submit、status、wait、manifest/Markdown 哈希校验、重复提交复用、视频/音频/封面 Range、短期播放链接和中间位置 Range。只保存 Markdown、manifest、脱敏 JSON 报告及临时播放页，不下载完整媒体。服务端看到的请求来源必须是 `192.168.31.78`；本机 .136 的结果不能作为跨设备验收。

若运行失败，保留返回的 job_id，继续 status/wait；不要换 URL 或幂等键盲目重交。保存的人工修改不能覆盖；内容不一致时换新的结果目录。

## 单步命令

```sh
node scripts/media-service.mjs health
node scripts/media-service.mjs submit 'https://v.douyin.com/Tg6ANQWIT7w/'
# 从 submit 输出取 job_id；下面为 mini 已有真实成功任务，迁移后保持有效
JOB_ID='386156d5bd76466de9e243878053f4e9'
node scripts/media-service.mjs status "$JOB_ID"
mkdir -p "$MEDIA_LAN_DIR/single-step"
node scripts/media-service.mjs wait "$JOB_ID" "$MEDIA_LAN_DIR/single-step/transcript.md"
node scripts/media-service.mjs fetch "$JOB_ID" "$MEDIA_LAN_DIR/single-step/transcript.md"
MEDIA_ID=$(node --input-type=module -e 'import fs from "node:fs";const m=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));console.log(m.media.video.path.split("/")[3])' "$MEDIA_LAN_DIR/single-step/transcript.md.manifest.json")
node scripts/media-service.mjs probe "$MEDIA_ID" video
node scripts/media-service.mjs probe "$MEDIA_ID" audio
node scripts/media-service.mjs probe "$MEDIA_ID" cover
node scripts/media-service.mjs playback "$MEDIA_ID" video
node scripts/media-service.mjs playback "$MEDIA_ID" audio
```

playback 输出的是最多 10 分钟有效的临时地址，可在浏览器打开；不用把地址返回聊天。过期后重新生成，不重复转写。

## 浏览器与返回结果

自动联调生成 `results/player.html`。在调用电脑浏览器打开（macOS 可 `open "$MEDIA_LAN_DIR/results/player.html"`），播放视频和音频，并分别拖动到约 8 分钟处，确认继续播放。页面会记录 loadedmetadata、playing 和 seeked 时间；临时地址过期需重新运行生成播放页。

请返回 `results/lan-report.json` 的脱敏内容，以及视频、音频播放及拖动结果。报告不含长期令牌、Cookie、原始私人响应或临时播放能力。收到报告并核验前，服务端维持“待联调”。

机器重启后的登录恢复、DHCP 地址保留仍需单独验证；不要把进程重启通过写成整机重启通过。Wiki 归档/检索没有纳入本次联调。
