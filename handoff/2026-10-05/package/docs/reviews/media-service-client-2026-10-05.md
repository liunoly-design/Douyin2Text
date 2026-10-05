# 局域网媒体客户端交付检查

范围：在 codex/wiki-v03 工作区新增独立客户端、CLI 和双方接口契约；基线 bd84f638f70a9745118cb8b5d7cf24a785074111。未改现有本地 Douyin pipeline、Vault、OpenClaw运行配置，未在mini部署。注册checkout已有文档修改保留。

## 测试证据

- Node v24.21.0。
- `node --test test/media-service-client.test.js`：12/12，通过真实回环HTTP与合成内容测试，并实跑CLI子进程。
- `npm test`：120/120。
- `.local/v070/f2-test/venv/bin/python -m unittest discover -s tests`：62/62。系统python首次运行缺bs4/httpx导致失败，切换已有隔离环境后通过，不修改系统依赖。
- `npm run plugin:validate`：isolated=true、loaded=true、hook=reply_dispatch。
- 两个JS入口语法检查、暂存差异空白检查通过。

测试覆盖：认证与稳定幂等键；任务查询及完整Markdown哈希；远程媒体/播放链接/Range；401错误脱敏；禁止重定向携带凭据；请求与轮询超时；人工介入状态；元数据缺失及异源路径拒绝；排他保存、人工修改及符号链接保护；同内容10次并发保存；中断临时文件及缺manifest恢复；CLI只取MD和manifest、不下载完整媒体。

## Standards

首轮1项：直接写最终文件影响并发幂等/失败恢复。已改为私有临时文件完整写入、fsync、硬链接原子排他发布，保留既有内容比对；复查0项未解决。

## Spec

首轮2项：manifest必填元数据未检查；最终文件中断恢复。已补来源/模型/媒体字段验证与完整fixture，采用上述原子发布；复查0项未解决。

## 证据边界

HTTP服务为合成fixture，不是F2/FFmpeg/Whisper实际服务。尚缺Mac mini服务端实现、模型准确率/性能、真实视频完整链路、DHCP保留、launchd恢复和另一台电脑跨设备联调。远程客户端为独立入口，尚未接入飞书或受保护Wiki发布。部署交接见 ../contracts/mac-mini-server-handoff.md，双方契约见 ../contracts/media-service-v1.md。
