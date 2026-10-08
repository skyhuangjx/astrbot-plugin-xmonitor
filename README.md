# AstrBot XMonitor 插件

在 AstrBot 中可视化监控 X 账号，并将新推文渲染成图片推送到群聊或私聊。

> 当前是发布前草稿。拟议仓库地址：<https://github.com/skyhuangjx/astrbot-plugin-xmonitor>

## 项目来源与致谢

本项目是基于 [Gu-Haojia/XMonitor](https://github.com/Gu-Haojia/XMonitor) 开发的 AstrBot 适配与扩展版本。

特别感谢原作者 **Gu-Haojia** 开发 XMonitor 的基础代码、X API v2 访问逻辑、推文解析模型和原始渲染实现。本项目保留上游 MIT License，并在此基础上增加了 AstrBot 插件入口、WebUI 配置、账号与会话连线、定时监控、游标持久化、AstrBot 图片渲染和平台消息发送适配。

这不是对上游项目的重新署名；上游来源和许可证见 [NOTICE](NOTICE) 与 [LICENSE](LICENSE)。

## 功能

- 在 AstrBot WebUI 中添加 X 账号和群聊/私聊目标；
- 用拖拽连线配置“账号 → 推送目标”关系；
- 按轮询间隔获取新推文；
- 支持 `none`、`translated`、`bilingual` 三种翻译模式；
- 支持 AstrBot HTML 渲染，服务器端默认不启动 Chromium；
- 支持本地 Playwright 渲染作为可选后端；
- 对头像等远程资源提供超时和首字母占位降级；
- 提供 `/xmonitor latest`、`/xmonitor check` 和 WebUI 测试入口。

## 安装

将整个目录复制到：

```text
AstrBot/data/plugins/astrbot_plugin_xmonitor/
```

然后在 AstrBot 插件页面重载插件。插件要求 AstrBot `>=4.27.4`。

依赖安装：

```bash
python -m pip install -r requirements.txt
```

## 配置与使用

1. 在插件配置中填写 X API v2 Bearer Token；
2. 设置轮询间隔；
3. 选择 `none`、`translated` 或 `bilingual`；
4. 选择 `astrbot` 或 `playwright` 渲染后端；
5. 打开插件详情页中的 **监控关系** 页面；
6. 添加 X 账号和推送目标，并从账号拖到目标建立连线；
7. 点击“保存关系”，监控任务会使用最新关系。

推送目标使用 AstrBot 原生会话字段：

```text
平台实例 ID（platform_id）
会话类型（message_type）
会话 ID（session_id）
```

在目标群或私聊中发送 `/xmonitor where`，复制对应字段。`unified_msg_origin` 是三者组成的完整会话来源，不要把它填入某一个字段。

## 命令

```text
/xmonitor <X 推文链接或 ID>  手动渲染一条推文
/xmonitor latest @用户名     获取用户最近一条推文
/xmonitor sample             本地示例，不需要 Token
/xmonitor where              显示当前 AstrBot 会话信息
/xmonitor status             查看监控状态
/xmonitor check              立即执行一次监控检查
```

右侧“立即检查”执行后台轮询，没有新推文时不会发送图片；卡片下方的“测试”发送固定示例图片。

## 服务器部署

2 核 2G 服务器建议将 `render_backend` 保持为 `astrbot`，使用 AstrBot HTML 渲染服务，避免在服务器上启动 Chromium。

本地电脑可以选择 `playwright`，并安装：

```bash
python -m pip install 'playwright>=1.45,<2'
python -m playwright install chromium
```

## 故障排查

如果日志先出现：

```text
Prepare to send ... [图片]
```

说明取推文和渲染通常已经完成。后续出现 `rich media transfer failed` 或 `retcode=1200 Timeout`，表示平台适配器没有及时返回富媒体发送确认；平台最终仍可能已经送达。先在同一目标发送普通小图片，以区分平台问题和插件图片大小/格式问题。

日志会使用以下来源标识：

```text
source=manual_latest  /xmonitor latest
source=manual_tweet   手动查询推文
source=web_test       WebUI 测试图片
source=web_check      右侧立即检查
```

每个操作都有独立编号和耗时，便于确认图片来源和失败阶段。

## 监控行为

- 新账号或新连线第一次只建立基线，不补发历史推文；
- 每个账号与目标的连线单独保存游标；
- 默认排除回复，保留转推；
- 每次账号检查最多读取 10 条新推文；
- 同一账号可以连接多个目标，同一目标也可以接收多个账号。

## 许可证

本项目使用 [MIT License](LICENSE)。本项目包含来自 [Gu-Haojia/XMonitor](https://github.com/Gu-Haojia/XMonitor) 的衍生代码，原项目的许可证和来源说明见 [NOTICE](NOTICE)。
