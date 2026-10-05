# 局域网媒体服务接口 v1

状态：2026-10-05 本机客户端开发契约。服务端须按此实现；合成 HTTP 测试不代表 Mac mini 已部署或真实转写验收通过。

## 范围与部署

Mac mini 保存视频、完整音轨、原始元数据和完整机器转写；调用电脑只取 Markdown/manifest，通过远程地址读媒体。服务与现有 Wiki 发布解耦，不自动写 Vault、不覆盖人工校订。此模式替代此前本地压缩后清理策略，远程正式视频和音频长期保留。

建议地址 `http://192.168.31.136:8765`，以实测及 DHCP 地址保留为准。原生 whisper.cpp 回环监听，外层网关提供持久化任务、认证、媒体读取。没有公网端口转发。所有接口要求 `Authorization: Bearer <token>`，令牌文件权限 0600。API 重定向禁止；错误及日志不得包含令牌、Cookie、原始私人响应。HTTP 适用于可信局域网，跨不可信网络用 HTTPS/受保护隧道。

## 通用结构

JSON UTF-8，未知新增字段可忽略。ID 匹配 `[A-Za-z0-9_-]{1,128}`。所有响应带 `api_version: "1"`（Markdown、媒体除外）。失败结构：

```json
{"api_version":"1","error":{"code":"AUTH_REQUIRED","message":"Authentication required","retryable":false}}
```

400 无效输入；401/403 认证；404 未找到；409 幂等键冲突或产物未就绪；413 超限；429/503 暂不可用。客户端不自动重试 POST，超时后使用原 Idempotency-Key 重交或查询；服务器必须原子去重。

## 接口

### GET /health

200：`{"api_version":"1","status":"ready"}`。加载模型/停工返回 503；可增加 downloader/model/storage 检查结果，不能把进程存活冒称处理就绪。

### POST /v1/jobs

必须带 `Idempotency-Key`（客户端按 URL + options 的 SHA-256 生成；调用者也可指定）。请求：

```json
{"url":"https://v.douyin.com/Tg6ANQWIT7w/","options":{"language":"zh","max_height":1080}}
```

只处理单条公开抖音视频，服务器验证 URL/所有跳转及下载目标，不允许访问内网、回环和任意文件。max_height=1080 指最高短边不超过 1080，不放大。超过 30 分钟或 1GB 暂停等待确认，本版没有远程批准接口，不能绕过已有边界。

202 新任务 / 200 复用：

```json
{"api_version":"1","job_id":"job_001","status":"queued","reused":false}
```

相同 key 和相同请求返回相同 job_id；不同请求同 key 返回409。URL 别名按平台 ID 再去重；视频哈希 + 模型哈希 + 转写配置确定产物版本，配置变化生成新版本，旧产物保留。

### GET /v1/jobs/{job_id}

200：`{"api_version":"1","job_id":"job_001","status":"transcribing"}`。
状态集合：queued/downloading/extracting/transcribing/succeeded/failed/partial_failed/waiting_login/waiting_confirmation。后四者为停止轮询、需要恢复或人工处理的状态，不能冒称成功。失败时可附上述 error 对象。

状态、阶段和产物路径持久化。重启后复用已核验阶段，原件不删。无法判定是否完成时核对文件/哈希后恢复，不能直接重复发布。单个 ASR worker，任务提交不等待推理完成。

### GET /v1/jobs/{job_id}/manifest

成功后200，未就绪409。必填示例：

```json
{"api_version":"1","job_id":"job_001","source":{"platform":"douyin","id":"7691977131957472558","url":"https://www.douyin.com/video/7691977131957472558","title":"平台原始描述","author":"作者","published_at":"2026-10-02T15:59:54+08:00","duration_seconds":535.197},"transcription":{"engine":"whisper.cpp","model":"medium","model_sha256":"64位小写十六进制哈希","language":"zh"},"markdown":{"sha256":"Markdown原始UTF-8字节的64位小写十六进制哈希"},"media":{"video":{"path":"/v1/media/media_001/video","sha256":"64位哈希","bytes":51022897,"mime":"video/mp4"},"audio":{"path":"/v1/media/media_001/audio","sha256":"64位哈希","bytes":1234,"mime":"audio/mp4"},"cover":{"path":"/v1/media/media_001/cover","sha256":"64位哈希","bytes":6398,"mime":"image/jpeg"}}}
```

示例哈希是占位说明，实际必须为真实 SHA-256。路径只能是本服务对应媒体路径，不能返回服务器文件系统路径。来源必须保留原始分享链接（可加 share_url）、规范来源、原始 JSON。缺失字段显式 null，不生成虚假值。封面缺失须报告 partial_failed，本版成功路径要求三种媒体齐全。

### GET /v1/jobs/{job_id}/markdown

200 `Content-Type: text/markdown; charset=utf-8`，返回完整机器稿与时间戳、平台标题/作者/来源、模型信息、媒体引用。UTF-8 字节哈希必须匹配 manifest。稳定媒体引用需要认证，不能把长期令牌或过期签名写入正式 Markdown。机器稿未经人工校订，不做自动摘要、分人、删除语气词。

### GET /v1/media/{media_id}/{video|audio|cover}

认证读取，正确 MIME、Content-Length、ETag、Accept-Ranges: bytes。支持单区间 Range 请求，合法区间返回206与准确Content-Range；超界416。路径遍历/符号链接越界拒绝。存储文件不可由接口覆盖/删除。

### POST /v1/media/{media_id}/playback

认证创建短期浏览器播放链接，请求 `{"kind":"video"}`（或audio），200：`{"api_version":"1","url":"http://192.168.31.136:8765/play/opaque-token","expires_at":"ISO8601时间"}`。同源、最多10分钟有效，支持 Range，不含长期 Bearer token。过期401/403；新建播放链接不重复处理视频。

## 客户端与联调

仓库 `src/media-service-client.js` 为本机客户端，`scripts/media-service.mjs` 为命令入口。Node >=24，无新增第三方依赖。转写引擎名称为非空字符串；默认 whisper.cpp，也支持符合统一结果格式的替代引擎，模型与 SHA-256 校验要求保持不变。环境变量 MEDIA_SERVICE_URL、MEDIA_SERVICE_TOKEN_FILE。命令：

```sh
node scripts/media-service.mjs health
node scripts/media-service.mjs submit 'https://v.douyin.com/Tg6ANQWIT7w/'
node scripts/media-service.mjs status JOB_ID
node scripts/media-service.mjs fetch JOB_ID /absolute/output/transcript.md
node scripts/media-service.mjs wait JOB_ID /absolute/output/transcript.md
node scripts/media-service.mjs playback MEDIA_ID video
node scripts/media-service.mjs probe MEDIA_ID video
```

fetch/wait 同时保存 transcript.md 与 transcript.md.manifest.json，排他创建，不覆盖既有内容；相同字节允许重复取回。若保存部分失败，重运行 fetch 核对后补齐。等待超时保留 job_id，可继续 status/wait，不重交任务。probe 只请求前16字节以验证远程 Range，不下载完整视频。

## 服务端交付要求

另一位 Codex 按本文实现上述全部接口；使用 F2 获取公开视频、FFmpeg 从完整视频提取音轨、原生 Metal whisper.cpp 本地中文转写。正式音轨与16kHz单声道 ASR 工作副本分离。数据 `/Users/mac/Documents/Personal-Wiki-Media/{video,audio,transcripts,metadata,state,logs,tmp}`；代码另目录。不得提交原始媒体或凭据。

固定 IP 使用路由器 DHCP 保留，核实网卡MAC、DHCP范围和冲突；mini优先192.168.31.136，其他电脑实测后生成绑定表。部署launchd，说明是否依赖登录；关闭终端后继续运行、异常重启、重启机器后恢复必须分别测试。

交付真实样本的完整解码、音轨时长、完整转写、原始封面、全部哈希、重复提交、重启恢复、鉴权、Range/拖动播放、播放链接过期测试。运行本仓库客户端从另一台电脑联调 health/submit/status/wait/playback/probe，返回脱敏报告；本机合成服务通过不能替代跨设备真实验收。不能把服务上线当作 Wiki 已发布/可全文检索；该集成另有受保护发布验收。


## 2026-10-05 真实样本语种补充（双方客户端与服务端同步）

指定样本音轨为英文。`transcription.language` 必须记录原音轨真实识别语种（此例 `en`），不能伪标 `zh`。`options.language=zh` 表示请求中文交付；英文音轨的完整原始 ASR 保留，同时追加带时间戳的完整中文机器译文，不能覆盖原文。

非中文原音轨的成功结果必须额外包含 `translation`：`kind=derived_machine_translation`、`source_language` 与原始语种相同、`target_language=zh`、真实 `model`/`model_sha256`、`coverage.source_segments` 正整数及 `coverage.all_source_segments_present=true`。记录翻译引擎、版本、原稿哈希、模型来源和耗时。翻译未完成返回 `partial_failed` 或 `waiting_confirmation`。

客户端保留中文 ASR 校验，并接受上述完整中文派生译文。除本项外保持原 v1 接口、状态集合、认证与幂等约束。原客户端只接受 language=zh，联调前必须使用本包更新后的客户端或同步相同检查与测试。
