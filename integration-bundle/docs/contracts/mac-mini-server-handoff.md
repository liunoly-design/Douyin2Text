# Mac mini 服务端开发交接

将本文件与 [media-service-v1.md](media-service-v1.md)、客户端和测试一并交给 Mac mini 上的 Codex。以 v1 契约为双方唯一接口依据；如果需要变更接口，先同步契约与本机客户端测试。

## 可直接复制的任务提示词

```text
请在本机 Mac mini 实际部署视频处理服务，严格实现附件 media-service-v1.md 的全部接口。

已授权：创建独立代码和数据目录、安装依赖和模型、配置 launchd、开放局域网服务、测试指定公开视频。先核对实际环境：Mac mini 2024 / 16GB / arm64 / macOS 26.5.2 / 用户mac / 当前IP192.168.31.136。不要把这些线索当作已验证结果。

代码目录 /Users/mac/Documents/Personal-Wiki-Media-Service；数据目录 /Users/mac/Documents/Personal-Wiki-Media，按契约分开video/audio/transcripts/metadata/state/logs/tmp。正式资料长期保留，现有服务和Vault保持完整。服务端不写调用电脑的Vault。

下载用 F2 https://github.com/Johnserf-Seed/f2 ，已实测参考commit 3f842b6b28bf70e5525631f7d992245b2dbccc0b（v0.0.1.8-pw3）。独立Python环境。优先公开访客 tt wid；需要登录时暂停，不读取其他应用私人Cookie。获取原始元数据和封面，选择最高短边不超过1080的视频。

FFmpeg从完整视频提取完整音轨，不能用平台music背景音乐替代。保留正式音轨，另制16kHz单声道PCM转写副本。

转写采用原生Metal whisper.cpp https://github.com/ggml-org/whisper.cpp ，参考commit 60c0be6ac8fa71b1a2ae2dd938a31a34a508e774。先试多语言small/medium，用实际中文效果、内存和耗时选型；模型哈希与版本写入manifest。whisper-server仅回环监听，外层局域网网关负责v1契约、持久队列、幂等、认证、媒体Range和Markdown。单ASR worker，不自动切换云端收费服务。

指定真实测试 https://v.douyin.com/Tg6ANQWIT7w/ ：此前获取ID7691977131957472558，作者飞鱼的AI世界，约535秒。必须本机重新跑通，不能把另一台电脑结果作为本机验收。

固定IP优先路由器DHCP保留，核对网卡MAC和冲突，mini优先192.168.31.136。其他电脑收集实际信息后给绑定表，不猜IP/网关/DNS。缺路由器信息时继续服务部署，单独报告地址保留未完成。

使用launchd常驻并说明登录依赖。令牌文件0600，不在聊天/日志中输出长期令牌。API地址、模型、真实数据不得随代码提交。仅开放局域网，不配置公网转发。

验收严格遵照契约：所有API、鉴权、重复提交、重启恢复、哈希、完整视频和音轨解码、完整中文带时间戳机器稿、Range及短期播放链接过期。然后使用提供的 scripts/media-service.mjs 从调用电脑做真实联调。本机合成测试与跨设备真实测试分别报告。

交付源代码、版本、模型信息、服务地址、认证文件位置、启动停止和日志命令、全部测试结果及未完成项。不能仅报告安装成功，也不能声称Wiki已归档或可检索。
```

## 调用电脑准备

环境变量示例（令牌由安全渠道写入本机文件，示例不含真实令牌）：

```sh
export MEDIA_SERVICE_URL='http://192.168.31.136:8765'
export MEDIA_SERVICE_TOKEN_FILE='/absolute/private/media-service.token'
chmod 600 "$MEDIA_SERVICE_TOKEN_FILE"
node scripts/media-service.mjs health
node scripts/media-service.mjs submit 'https://v.douyin.com/Tg6ANQWIT7w/'
# 将返回的job_id替换到下面，输出目录须已存在
node scripts/media-service.mjs wait JOB_ID /absolute/output/transcript.md
# 从manifest.media.video.path提取MEDIA_ID
node scripts/media-service.mjs probe MEDIA_ID video
node scripts/media-service.mjs playback MEDIA_ID video
```

输出 Markdown 与旁边 manifest；视频仍保存在 mini。playback 返回临时链接可在浏览器播放，正式 Markdown 的稳定媒体引用需要认证。服务未部署时以上命令预期失败，不表示本机代码失效。
