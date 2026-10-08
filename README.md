# AstrBot XMonitor 插件

在 AstrBot 中可视化监控 X 账号，并将新推文渲染成图片推送到群聊或私聊。
<img width="900" height="777" alt="E2D7F2BB74695F13016EFD0A70405717" src="https://github.com/user-attachments/assets/fcce6412-4176-4352-82fb-013fef2a4a18" />


在原版 XMonitor 的基础上增加了嵌入 AstrBot WebUI：监控账号和推送目标以卡片展示，添加账号、添加群聊或私聊目标一目了然。用鼠标连线就能配置推送关系，直观、简洁，方便管理。

**连线即订阅推送，点击线即取消推送；修改后点击“保存关系”生效。**

## 项目来源与致谢

本项目是基于 [Gu-Haojia/XMonitor](https://github.com/Gu-Haojia/XMonitor) 开发的 AstrBot 适配与扩展版本。

特别感谢原作者 **Gu-Haojia** 开发 XMonitor 的基础代码、X API v2 访问逻辑、推文解析模型和原始渲染实现。本项目在此基础上增加了 AstrBot 插件入口、WebUI 配置、账号与会话连线、定时监控、游标持久化、AstrBot 图片渲染和平台消息发送适配。

## GUI：看得见的推送关系<img width="3320" height="2102" alt="QQ20261008-184504" src="https://github.com/user-attachments/assets/5c4869d8-8792-4edf-8f7e-3574ae2da006" />


页面左侧是监控账号，中间是群聊/私聊目标，右侧是“保存关系”和“立即检查”。每个卡片只显示名称，连线关系一眼就能看清。
<img width="3172" height="1206" alt="QQ20261008-184531" src="https://github.com/user-attachments/assets/5b3abb99-94ac-4b1a-96f7-5f900be07d10" />

| 操作 | 效果 |
| --- | --- |
| 添加账号 | 输入 X 用户名和显示名称，生成账号卡片 |
| 添加目标 | 填写群聊或私聊的 AstrBot 会话信息，生成目标卡片 |
| 从账号卡片拖到目标卡片 | 建立推送关系；账号的新推文会推送到该目标 |
| 点击已有连线 | 删除该条推送关系，取消向该目标推送 |
| 双击卡片 | 在下方表单编辑账号或目标 |
| 鼠标悬停卡片 | 查看用户名、会话信息及连线数量等详情 |
| 卡片变绿 | 表示该卡片已有连线；账号还需启用监控 |
| 点击“保存关系” | 将当前账号、目标和连线保存给后台监控任务 |

例如，A、B 两个账号需要推送到 1 群，就将 A、B 分别连到 1 群；C 只推送到 2 群，就将 C 连到 2 群。一个账号可以连多个群，一个群也可以接收多个账号。

新建连线首次检查只建立基线，之后推送新推文。想马上查看最新一条，使用 `/xmonitor latest @用户名`。

## 功能

- 在 AstrBot WebUI 中添加 X 账号和群聊/私聊目标；
- 用拖拽连线配置“账号 → 推送目标”关系；
- 按轮询间隔获取新推文；
- 支持 `none`、`translated`、`bilingual` 三种翻译模式；
- 支持 AstrBot HTML 渲染，服务器端默认不启动 Chromium；
- 支持本地 Playwright 渲染作为可选后端；
- 头像加载失败时显示用户首字；本地 Playwright 的图片加载等待设有上限；
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

## Bot 命令与测试方法

在与 Bot 的群聊或私聊中发送以下命令。`/xmonitor` 也可简写为 `/x`。

| 命令 | 用途 |
| --- | --- |
| `/xmonitor` 或 `/xmonitor help` | 查看命令帮助 |
| `/xmonitor where` | 查看当前会话的 `platform_id`、`message_type`、`session_id`，用于添加推送目标 |
| `/xmonitor status` | 查看 Token 是否已配置、后台任务状态、账号/目标/连线数量、上次检查时间和错误 |
| `/xmonitor sample none` | 在当前会话发送内置示例图片，测试渲染和发送；不需要 X Token，也不调用翻译 |
| `/xmonitor test none` | 同上，是示例测试的别名 |
| `/xmonitor latest @用户名` | 查询并发送该账号最新一条可用推文，验证 X API、渲染和发送；不要求已添加监控关系 |
| `/xmonitor <X 推文链接或 ID>` | 查询并发送指定推文图片 |
| `/xmonitor check` | 按已保存的监控关系立即检查新推文，并返回监控状态 |

推荐按以下顺序验证安装：

1. 发送 `/xmonitor sample none`，确认 Bot 能渲染并发送示例图片；
2. 发送 `/xmonitor where`，将会话字段填入 GUI 的推送目标；
3. 双击目标卡片，点击下方“测试”，确认示例图片发送到所选目标；
4. 发送 `/xmonitor latest @用户名 none`，验证真实推文查询；
5. 添加账号、建立连线并“保存关系”，点击“立即检查”建立基线；之后的新推文按轮询间隔推送；
6. 使用 `/xmonitor status` 查看后台状态和错误。

右侧“立即检查”和 `/xmonitor check` 执行后台轮询，只有新推文才会发送，首次建立基线或没有更新时不会发送图片。卡片下方的“测试”发送固定示例图片到所选目标；命令示例和手动推文查询发送到命令所在的当前会话。

推文命令末尾可添加 `none`（原文）、`translated`（仅翻译）或 `bilingual`（双语）临时覆盖配置，例如：

```text
/xmonitor latest @openai none
/xmonitor latest @openai bilingual
/xmonitor https://x.com/openai/status/推文ID translated
```

`translated` 和 `bilingual` 使用 AstrBot 当前会话的 LLM provider，需要先配置可用模型。

## 服务器部署

如服务器配置不高，建议将 `render_backend` 保持为 `astrbot`，使用 AstrBot HTML 渲染服务，避免在服务器上启动 Chromium。

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
