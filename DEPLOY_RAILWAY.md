# 部署口播稿助手到 Railway

本项目需要两个 Railway 服务，使用同一个仓库。机器人服务保存任务状态；定时服务只负责按时触发它。

## 1. 常驻机器人服务

1. 将本目录放入一个**私有** GitHub 仓库，并在 Railway 新建服务连接该仓库。
2. Railway 会识别根目录的 `Dockerfile`。服务的默认启动命令已经是机器人 Web 服务。
3. 在 Railway 的 Variables 中填入现有 `.env` 的必要密钥：
   `FEISHU_APP_SECRET`、`ANTHROPIC_API_KEY`、`TRANSCRIPT_API_KEY`、
   `TRANSCRIPT_API_SECRET`、`HEYGEN_API_KEY`、`HEYGEN_AVATAR_ID`，以及
   `FEISHU_VERIFICATION_TOKEN`。另新增一个长随机值 `JOB_TRIGGER_SECRET`。
   不要上传 `.env` 文件。
4. 部署完成后生成公开域名；健康检查路径为 `/`。
   为此服务挂载 Railway Volume 到 `/data`，并新增 `STATE_DIR=/data`。
5. 在飞书开发者后台的“事件与回调”选择 Webhook，并填：
   `https://你的-railway-域名/feishu/events`。
   填入同一个 `FEISHU_VERIFICATION_TOKEN`，先不要开启加密策略。
6. 添加事件 `im.message.receive_v1`（接收消息），创建新版本并发布、通过企业管理员审核。

群聊内 @口播稿助手 后可发送：

- `帮助`
- `状态`
- `跑一条`
- `表格`

## 2. 每天运行一次的定时服务

在同一 Railway 项目中新建第二个服务，也连接相同仓库。

- 将 Start Command 设为：`python trigger_daily.py`
- 将 Cron Schedule 设为：`0 1 * * *`

为定时服务添加两个 Variables：

- `JOB_TRIGGER_SECRET`：与机器人服务的值相同。
- `BOT_INTERNAL_URL`：机器人服务的 Railway 内网 URL，例如 `http://${{口播稿助手.RAILWAY_PRIVATE_DOMAIN}}`。

Railway Cron 使用 UTC，因此 `0 1 * * *` 对应北京时间每天 09:00。该服务会处理一条未完成的来源链接，完成后自行退出。

## 注意

- 任务状态只保存在机器人服务的 `/data` 卷中；定时服务不会直接运行转写，因此不需要共享卷，也不会因临时容器重启而重复提交转写任务。
- 当前机器人能回复、报状态和触发“转写 + 两版改写 + 飞书回填”。HeyGen 自动生成仍是后续独立步骤，不能把“改写完成”当作“视频已生成”。
