# Aaron Reader

[English](README.md)

[Cloudflare 线上站点](https://aaron-reader.aaron-he-zhu.workers.dev/) ·
[GitHub 开源仓库](https://github.com/aaron-he-zhu/aaron-reader)

一个本地运行、确定性抓取、默认使用 **0 LLM token** 的博客订阅器。它目前订阅：

- [OpenAI News](https://openai.com/news/rss.xml)
- [OpenAI Developer Blog](https://developers.openai.com/blog)
- [Claude Blog](https://claude.com/blog/)
- [Anthropic News](https://www.anthropic.com/news)

正常同步完全不调用 OpenAI、Anthropic 或任何其他模型 API，也不需要 API key。抓取、条件缓存、页面解析、URL 规范化、内容指纹、去重、SQLite 持久化、未读管理、通知和 Markdown 摘要都由固定程序完成。

## 立即使用

项目没有第三方运行时依赖，macOS 自带的 Python 3.9 即可：

```bash
./aaron-reader sync
./aaron-reader status
./aaron-reader list --unread
./aaron-reader serve --open
```

英文是规范界面和默认语言。命令行、诊断信息、生成的 HTML/JSON/RSS/摘要以及本地阅读页都提供简体中文。单次命令可使用下面任一形式：

```bash
./aaron-reader --language zh-CN status
./aaron-reader status --language zh-CN
```

若要让当前 shell 持续使用中文，可设置环境变量：

```bash
AARON_READER_LANG=zh-CN ./aaron-reader status
```

语言优先级依次为 `--language`、`AARON_READER_LANG`、`config/sources.json` 中的 `default_language`，最后回退到英文。阅读页也有可见的 English / 简体中文选择器，并会记住浏览器选择。这些选项只翻译阅读器界面；文章标题与发布方简介始终保持原文，因此多语言支持不会增加后台翻译、模型调用或 LLM token 消耗。

请使用仓库根目录的 `./aaron-reader` 包装脚本。配置和运行数据也以本源码树为基准，因此本项目不把 `pip install` 后生成的全局命令当作受支持的运行方式。

第一次 `sync` 会建立历史基线。基线文章写入数据库但自动标为已读，也不会发送大量旧文章通知。之后同步发现的真正新文章才保持未读并触发 macOS 通知。

阅读页默认生成在 `public/index.html`。`serve --open` 会在 `127.0.0.1:8765` 启动只读本地服务；它不会暴露到局域网。服务只允许读取五个生成物，不会列目录或跟随符号链接。若确实要让局域网访问，必须显式使用 `--allow-network`，例如 `serve --host 0.0.0.0 --allow-network`。此模式没有身份验证，请只在可信网络使用。可选 AI 按钮属于下文说明的另一种、必须显式开启且仅限 loopback 的模式，不能与 `--allow-network` 同时使用。

## 定时同步

当前这份工作目录默认使用 ChatGPT/Codex 桌面客户端的 Scheduled 任务，而不是
launchd。已启用的项目任务名为 **Aaron Reader twice-daily sync**：每天在
`America/Los_Angeles` 时区的 10:00 与 22:00 运行，模型固定为
`gpt-5.6-luna`，推理强度为 `medium`。每轮最多为 3 篇新未读文章一次性生成中文
摘要和翻译。订阅抓取、待处理输入选择、严格结果与指纹校验、原子缓存、健康检查、
公开状态脱敏、前端 lint/type/build/test 以及精确快照 commit 都由固定程序完成。
任务随后把该 commit 推送到公开 GitHub 仓库，由 Cloudflare Workers Builds 对这份
已推送源码进行构建和发布。普通同步与发布不会调用 OpenAI API；仓库中也不保存
模型 API key 或 Cloudflare 凭据。

定时任务需要访问本地项目时，电脑必须保持开机且 ChatGPT 桌面客户端需要运行。
GitHub 推送或 Cloudflare 构建失败时，线上会继续保留上一个成功的 Worker 版本。

下面的 LaunchAgent 流程仍作为可选的本地兜底方案保留。不要同时启用它和 Codex
任务，否则两个调度器会同时写阅读器数据库与生成文件。当前安装中的 LaunchAgent
已经停用；卸载只移除了调度入口，旧安装运行环境和数据库都保留。

在 macOS 上，以当前登录用户安装每小时运行一次的 LaunchAgent。**不要使用 `sudo` 或 root。** 通知、`~/Library/LaunchAgents` 和 GUI launchd domain 都属于当前登录用户：

```bash
./scripts/install-launchd.sh
```

安装器会先运行离线 `doctor`；随后把无第三方依赖的程序和配置安装到适合后台访问的 `~/Library/Application Support/Aaron Reader`，并在首次安装时创建一致的 SQLite 备份。这样，即使源码仓库位于 Documents、Desktop 或 Downloads，也无需给 macOS 开启“完全磁盘访问”。重装会更新程序和配置，但保留安装态数据库。安装器会在 `~/Library/LaunchAgents` 生成临时 plist，通过系统 `plutil` 校验，再原子替换正式 plist。重装时会先备份旧 plist；如果新任务无法加载，安装器会尽量回滚到旧任务。plist 使用 `RunAtLoad`，成功加载后由 launchd 自动开始首轮同步，不会额外强制杀掉并重启一次。

也可以传入秒数。例如每 30 分钟同步一次：

```bash
./scripts/install-launchd.sh 1800
```

查看或卸载任务：

```bash
./scripts/status-launchd.sh
./scripts/uninstall-launchd.sh
```

定时订阅使用的当前阅读页和命令位于 Application Support：

```bash
open "$HOME/Library/Application Support/Aaron Reader/public/index.html"
"$HOME/Library/Application Support/Aaron Reader/aaron-reader" status
```

修改当前仓库的源码或 `config/sources.json` 后，请重新运行 `install-launchd.sh` 更新已安装运行环境。定时任务使用的数据库、已读/收藏状态、缓存输出和日志都保留在 Application Support，重装不会覆盖。

生命周期脚本默认显示英文消息。若要显示简体中文，请设置 `AARON_READER_LANG=zh-CN`，例如：

```bash
AARON_READER_LANG=zh-CN ./scripts/status-launchd.sh
```

生成的 LaunchAgent 会显式设置 `AARON_READER_LANG=en`，因此即使安装器使用中文消息，无人值守运行产生的日志仍固定为英文。

`status-launchd.sh` 同时检查 plist、launchd 注册状态、上一次定时任务退出码、已安装运行环境是否属于当前仓库，以及订阅源的严格健康状态；任一检查失败时返回非零。运行环境会记录源码仓库的绝对路径，因此移动或重命名项目目录后，必须在新位置重新运行 `install-launchd.sh`。

卸载时 plist 会移到废纸篓，已安装运行环境、数据库、文章、静态输出和日志都不会被删除。标准输出保存在 `~/Library/Application Support/Aaron Reader/data/launchd.log`，错误和警告保存在同目录的 `launchd.error.log`。launchd 不会替本项目轮转这两个文件；长期运行时请定期检查、归档或清理日志。

### GitHub 与 Cloudflare 发布边界

公开生产站点是
[aaron-reader.aaron-he-zhu.workers.dev](https://aaron-reader.aaron-he-zhu.workers.dev/)。
Cloudflare Workers Builds 跟踪 GitHub `main` 分支，从 `/site` 构建，并且只在
GitHub push 成功后发布；非生产分支构建已经关闭。

`site/` 是当前仓库内的 vinext/Cloudflare Worker 发布面，只包含只读双语界面，
以及 JSON、RSS 和 Markdown 的明确公开投影。进入 Git 历史或 Cloudflare 前，
固定程序会移除个人已读、收藏、待处理计数与原始错误信息。这里没有 SQLite
数据库、API key、D1/R2/Images 绑定或托管 AI 接口；界面默认显示英文，并提供
简体中文切换。

手动执行与定时任务相同的固定准备流程：

```bash
./scripts/prepare_cloudflare_release.py
```

最后一行 JSON 只会是 `unchanged`、`ready` 或 `failed`。命令会同步、校验并脱敏
公开快照，运行完整网站检查，并最多创建一个精确的快照 commit；它不会直接联系
Cloudflare。配置好公开 GitHub `origin` 与 Cloudflare Workers Builds 后，可由同一
固定程序推送已验证的 `main` commit：

```bash
./scripts/prepare_cloudflare_release.py --push
```

Cloudflare 只从 `site/` 中已经提交的文件构建；全新 clone 不依赖被忽略的本地数据库
或根目录 `public/`。`codex://` 按钮的可选本地路径只保存在访问者浏览器中，不进入
Git 或部署快照。

## 常用命令

```bash
# 同步单个来源
./aaron-reader sync --source claude-blog

# 忽略本地保存的 HTTP validator 和响应体哈希
./aaron-reader sync --force

# 第一次导入时也保留历史为未读
./aaron-reader sync --keep-history-unread

# 列表、搜索和收藏过滤
./aaron-reader list --limit 30
./aaron-reader list --unread --source anthropic-news
./aaron-reader list --query Codex
./aaron-reader list --starred

# 阅读状态与收藏
./aaron-reader read 12 13
./aaron-reader unread 12
./aaron-reader read --all
./aaron-reader star 12
./aaron-reader unstar 12

# 固定程序生成 Markdown，不调用 LLM
./aaron-reader digest

# 适合监控脚本：任一来源未同步、过期、降级或异常时返回非零
./aaron-reader status --strict

# 验证配置和数据库；--live 会联网做解析契约检查但不写文章
./aaron-reader doctor
./aaron-reader doctor --live
```

可用的 source slug：

- `openai-news`
- `openai-developers`
- `claude-blog`
- `anthropic-news`

## 可选 AI 摘要与翻译

Aaron Reader 现在包含完整但**默认关闭**的 AI 增强旁路。正常的 `sync`、`status`、`list`、`render`、确定性 `digest`、通知、LaunchAgent 和普通页面浏览仍然不会发出模型请求，也不需要 API key。AI 结果写在独立表中，绝不会覆盖发布方标题或简介。

### ChatGPT/Codex 订阅桥接（不需要 API key）

推荐的无人值守路径可以直接使用 ChatGPT/Codex 桌面任务所选的模型，而不是让
Python 程序发出 API 请求。固定程序只导出仍缺少 AI 结果、且长度受限的发布方
元数据：

```bash
./aaron-reader ai subscription-export --unread --limit 3 --to zh-CN \
  --output data/subscription-ai-request.json
```

命令会原子写入紧凑 JSON，并在 stdout 输出一个很小的状态对象，其中包含
`pending_count`、`request_path` 和 `suggested_result_path`。Codex 任务读取请求，
遵守其中内置的 instructions 与 schema，并把每篇文章合并后的摘要/翻译结果写到
建议结果路径。`pending_count` 为 0 时无需运行模型或导入。完成后执行：

```bash
./aaron-reader ai subscription-import \
  data/subscription-ai-request.results.json --json
```

网页动作或一次性的 Codex 请求可以准确指定文章，并且只导出用户点击的任务。
两个参数都可以重复使用；不写 `--task` 时仍保持原来的“摘要和翻译一起生成”行为：

```bash
# 只总结第 110 篇；即使它已经标为已读也可以处理
./aaron-reader ai subscription-export --article-id 110 --task summary \
  --to zh-CN --output data/subscription-ai-request.json

# 只翻译第 110 篇
./aaron-reader ai subscription-export --article-id 110 --task translation \
  --to zh-CN --output data/subscription-ai-request.json
```

结果契约仍同时保留 `summary` 和 `translation` 字段，但未请求的字段必须为
`null`；擅自返回未请求的结果会被拒绝。所选任务会写入文章指纹，而默认两任务
指纹保持向后兼容。

线上阅读器把历史回填保留为显式操作，避免低-token 默认流程静默处理全部基线
文章。页面上的 **补齐接下来 3 篇中文内容** 会打开一个有界 Codex 任务，等价于：

```bash
./aaron-reader ai subscription-export --all --limit 3 --to zh-CN \
  --output data/subscription-ai-request.json
```

导出器会跳过仍然有效的缓存，并保证本轮最多选择三篇；它不会改变每天两次的
`--unread` 策略。

日／周总结按钮使用独立的订阅请求。`daily` 指
`America/Los_Angeles`（旧金山）当天 00:00 至导出时刻；`weekly` 指旧金山
本周一 00:00 至导出时刻。夏令时切换由系统时区数据库正确处理。

```bash
./aaron-reader ai subscription-report-export --period daily --to zh-CN \
  --output data/subscription-daily-request.json
./aaron-reader ai subscription-report-import \
  data/subscription-daily-request.results.json --json

./aaron-reader ai subscription-report-export --period weekly --to zh-CN \
  --output data/subscription-weekly-request.json
./aaron-reader ai subscription-report-import \
  data/subscription-weekly-request.results.json --json
```

报告复用现有严格 digest schema，最多包含 50 条有界元数据，并按周期、文章
集合与内容、语言、模型、prompt、schema 和生成设置缓存。导入器会在写入 digest
及其持久报告记录的同一个事务中，重新核对时间窗口和每篇文章版本。最近的日／周
缓存通过 `public/latest.json` 的 `ai_reports` 字段提供给网页。

线上界面只显示与当前 English 或简体中文界面严格同语言的缓存报告，不会再把
中文报告静默塞进英文页面。日／周报告中的逐篇短摘要仍属于该期 digest，不会被
冒充为经过独立校验的文章级摘要或翻译缓存。

导出与导入不会检查 `OPENAI_API_KEY`，不要求 `ai.enabled=true`，不会构造 API
Provider，也不占用 API 旁路预算。导入器会拒绝额外字段、重复 JSON key、无效的
摘要/翻译结构、被重新排序的批次，以及导出后发生变化的文章、输入、prompt、
schema、模型或生成配置指纹。两类结果在同一个事务中写入；重复导入同一份有效
结果只会命中本地缓存。

OpenAI 集成使用 Responses API、严格 Structured Outputs、`store: false`、禁用工具，并让每篇文章成为独立请求。所有 AI 功能默认使用成本敏感的 `gpt-5.6-luna`，推理强度默认为 `medium`；AI 全局开关仍保持关闭，只有显式启用后才会调用模型。模型默认值只是配置，不代表服务商永远不变；修改前请查看当前的 [OpenAI 模型指导](https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p6-sol.md)。

### 1. 零模型调用预览

`preview` 会显示准确且有上限的输入对象、字符/字节数、保守 token 预留、缓存键、缓存状态和当前预算；即使 AI 仍关闭，它也不会构造 Provider 或调用模型：

```bash
./aaron-reader ai preview 12 --task summary --to zh-CN
./aaron-reader ai preview 12 --task translation --to zh-CN --field title
./aaron-reader ai status
```

### 2. 显式开启模型调用

编辑 `config/sources.json` 中已经存在的 `ai` 对象：

```json
{
  "ai": {
    "enabled": true,
    "provider": "openai",
    "translation_model": "gpt-5.6-luna",
    "summary_model": "gpt-5.6-luna",
    "digest_model": "gpt-5.6-luna",
    "reasoning_effort": "medium",
    "store": false,
    "input_policy": "metadata_only",
    "features": {
      "summary": true,
      "translation": true,
      "digest": true,
      "full_text": false,
      "web_actions": false
    }
  }
}
```

API key 只放在进程环境或当前 shell 的密码管理器集成中；不要把它加入 JSON、SQLite、plist、HTML 或命令参数：

```bash
export OPENAI_API_KEY='...'
```

程序只接受固定的 `OPENAI_API_KEY` 环境变量名和固定的 `https://api.openai.com/v1/responses` 端点。错误与审计记录不会保存请求体、Authorization header、API key 或完整 Provider 错误响应体。

### 3. 按需生成

```bash
# 直接生成中文摘要：只调用一次，不会先英文摘要再二次翻译
./aaron-reader ai summarize 12 --to zh-CN

# 只翻译发布方元数据；原文始终保留且可见
./aaron-reader ai translate 12 --to zh-CN --field title --field publisher-summary

# 对有限数量的未读文章生成一次结构化 AI 日报
./aaron-reader ai digest --unread --limit 20 --to zh-CN
```

第一次成功结果写入 `ai_artifacts`。当规范化后的准确输入、语言、模型、prompt、schema、生成参数和提取器版本都相同时，再次执行会直接命中本地缓存，不发送 Provider 请求。修改已读或收藏状态不会让缓存失效；发布方内容、目标语言、模型、prompt、schema、请求字段或已提取正文变化时会生成新缓存键。

### 4. 可选全文摘要

全文属于独立的确定性抓取步骤。`sync` 永远不会抓全文，原始 HTML 永远不会发给模型；只有同时开启功能和按需策略后才可使用：

```json
{
  "ai": {
    "input_policy": "fetch_on_demand_cached_local",
    "features": {
      "summary": true,
      "translation": true,
      "digest": true,
      "full_text": true,
      "web_actions": false
    }
  }
}
```

然后执行：

```bash
./aaron-reader ai fetch 12
./aaron-reader ai summarize 12 --full-text --to zh-CN
```

`fetch_on_demand_ephemeral` 不把已提取正文写入 SQLite；它只用于立即执行的 CLI 或本地网页请求，不能加入批处理队列，显式重试时会重新抓取正文。`fetch_on_demand_cached_local` 会保存可复用的本地快照；只有文章规范 URL 与提取器版本仍一致时才会复用。抓取器只接受文章所属来源配置中的精确发布方主机，对每个 DNS 结果和重定向逐跳重新校验，拒绝 localhost、私网、link-local、reserved 地址和 URL 凭据，不发送 cookie 或 Authorization，只接收有大小上限的 HTML，去除脚本、导航、表单与广告，规范化正文，并记录 hash、提取器版本、最终 URL 和截断状态。文章正文始终是不可信数据，不能给模型增加工具或指令。

系统有意不实现整篇文章翻译：它会显著增加 token、版权和分块失败风险。请只翻译标题/发布方简介，或直接用目标语言总结已提取正文。

### 5. 硬预算、队列与审计

每一次可能计费的尝试都有独立且持久的 `ai_attempts` 审计记录。发起网络请求前，SQLite 会用 UTF-8 字节上界加协议余量预留输入，再加上配置的最大输出，并同时检查每日与每月的 request/token 上限；两个并发进程不能同时花掉同一份剩余预算。预算为零会阻止调用。美元预算是可选能力，只有为每个已选模型在 `ai.prices` 中加入经过确认的价格快照后才启用；每份快照必须同时包含有限、非负的普通输入、输出、缓存读取输入和缓存写入输入费率。应按当前输入上限填写保守的最高有效费率，并计入适用的长上下文或服务等级倍率；程序只能校验数字结构，无法替你确认外部定价是否准确。Aaron Reader 不会把随时可能变化的价格硬编码进程序。

批处理是有上限、可独立缓存的单篇文章任务，而不是把许多文章藏进一个 prompt。worker 有意保持串行（`concurrency` 必须为 `1`），因此开启队列也不会意外形成多路并发模型调用：

```bash
# 还需先设置 ai.batch.enabled=true
./aaron-reader ai batch --unread --limit 10 --task summary --to zh-CN --yes
./aaron-reader ai worker --limit 10

./aaron-reader ai status
./aaron-reader ai audit --limit 100
```

Aaron Reader 绝不会自动重放结果不确定的 Responses POST。HTTP 429 会进入 `permanent_failed`，只有显式操作才会用新的审计请求重试。HTTP 408/409/425、5xx、超时和断线可能已经到达 Provider，因此会进入 `unknown`、保留保守预算预留，并要求额外确认风险后才能发出新请求。只有当 Provider 明确报告完整 usage 且输入可复现时，本地结构化输出校验失败才会进行短暂且有次数上限的重试。若 incomplete、refusal 或无输出响应带有完整 usage，审计会按该 usage 记账；若 usage 或 GPT-5.6 的缓存读取/缓存写入明细缺失或畸形，系统会继续保留原预算预留，不会假设成本为零。

```bash
# 重试 HTTP 429 等确定失败
./aaron-reader ai retry 37 --yes

# 只有在接受未知请求可能已计费后才重试
./aaron-reader ai retry 37 --allow-unknown --yes
```

worker 启动时会安全释放从未进入 “sent” 的旧 reservation；已经进入 sent 的旧请求只会转成 `unknown`。AI 失败不会改变订阅源健康状态、不会把文章标为已读，也不会阻塞确定性同步。

删除旧缓存结果但保留 usage/hash 审计记录：

```bash
./aaron-reader ai purge --before 2026-01-01 --yes
./aaron-reader ai purge --before 2026-01-01 --keep-snapshots --yes
```

### 6. 可选本地页面按钮

先设置 `ai.features.web_actions=true`，保持 `ai.enabled=true`，并给服务进程提供 `OPENAI_API_KEY`，随后显式启动：

```bash
./aaron-reader serve --open --enable-ai-actions
```

只有本次服务运行中的动态页面才会显示摘要/翻译按钮；普通 `render` 和磁盘上的 `public/index.html` 始终是被动页面，因此停止服务后不会遗留可调用的控件。页面加载只做本地 session 初始化，不会生成内容。点击后，浏览器只把有上限的文章 ID、任务和语言提交给 Python 后端；浏览器不能选择 Provider、模型、端点、prompt、URL 或 API key。写接口同时要求服务和客户端都在 loopback、Host 精确匹配、POST 同源、每次启动随机 CSRF、4 KiB 以下的严格 JSON，以及用于识别重复提交且长度受限的客户端请求 ID。`--allow-network` 与 AI 动作会被拒绝同时使用。

普通静态 HTML 和 `latest.json` 仍可显示已经缓存的 AI 结果，并明确标注 AI 生成、目标语言、元数据/全文依据、生成日期和截断状态。仅浏览、筛选或切换界面语言绝不会调用模型。

## 给 LLM 的最小输入包

如果以后确实需要模型生成中文总结，不要重新把整个网页交给模型。先让本程序只导出未读文章的标题和发布方简介，并设置硬字符预算：

```bash
./aaron-reader packet --max-chars 6000 > /tmp/aaron-reader-packet.json
```

`packet` 自身不调用模型。`character_budget` 覆盖整个格式化 JSON（包括外层字段与换行），`character_count` 和 `utf8_bytes` 是实际序列化大小；`approx_tokens` 只是粗略估计。不同 tokenizer 的结果不同，所以程序不会伪装成精确计费器。同一篇文章不会因同步而重复进入数据库。推荐流程是：

1. 固定程序同步、去重、关键词搜索和筛选。
2. 人先看标题与官方简介。
3. 仅在确有必要时，把 `packet` 的小对象一次性交给 LLM。
4. 完成后用 `read` 标记，下一次 packet 不再包含它。

## 输出与数据

- `data/reader.sqlite3`：文章、来源状态、HTTP validator、持久待处理队列、通知 outbox 和同步历史。
- `data/reader.sqlite3` 还保存 AI job、逐次 usage/reservation、缓存 AI 结果、持久日／周报告记录，以及仅在全文本地缓存策略下保存的规范化正文快照；API key 永远不会写入数据库。
- `public/index.html`：全库精确计数，可搜索、按来源和未读过滤；文章卡片显示最近 500 篇。
- `public/latest.json`：供其他固定程序消费的最近 500 篇，明确返回数和省略数，并通过 `ai_reports` 提供每种语言最近的日／周缓存报告。
- `public/feed.xml`：四个来源合并后的最近 100 篇本地 RSS。
- `public/digest.md`：最近 100 篇未读文章的确定性 Markdown 摘要，并明确全库未读总数。

这些运行产物默认被 `.gitignore` 排除。输出采用临时文件加原子替换；同步使用进程锁和 SQLite WAL，重复运行是幂等的。

备份正在运行的 WAL 数据库时，不要只复制 `reader.sqlite3` 主文件。可先停止 LaunchAgent 后复制数据库及 `-wal` 和 `-shm` 文件，或使用 SQLite backup API。最简单的个人备份方式是先卸载定时任务、确认没有 `sync` 在运行，再复制整个 `data` 目录。

## 抓取策略

| 来源 | 固定入口 | 增量与去重 |
|---|---|---|
| OpenAI News | 官方 RSS 2.0 | 对完整 RSS 做 `guid` 和 canonical URL diff；数据库展示回填与新 URL 发现分离 |
| OpenAI Developer Blog | 官方轻量 `blog.md` 索引 | HTTP ETag/Last-Modified 加全量 Markdown 链接 diff；HTML 列表补充日期，失败会持续重试 |
| Claude Blog | HTML 列表加官方 sitemap | 首页 validator；每日 sitemap URL set diff；仅新 URL 才补抓文章 JSON-LD/OG |
| Anthropic News | HTML 列表加官方 sitemap | 每日 sitemap URL/lastmod set diff；新 URL 补抓详情，lastmod 变化只刷新、不重复通知 |

Claude 与 Anthropic 第一次运行只把 sitemap 历史 URL 登记为已知，不会逐页抓取详情。以后即使首页只展示有限条目，sitemap 差分仍能补上机器停机期间出现的新 URL。只接受精确的 `/blog/<slug>` 或 `/news/<slug>` 路径；栏目页、分类页和异常重定向不会被当作文章。

所有新 URL 和 lastmod 变化会先写入 SQLite 持久队列，再按有限批次补全：sitemap 每轮最多 25 条，直接 feed 每轮最多 200 条。详情页临时失败、进程退出、响应体未变化或下次 sitemap 返回 `304`，都不会抹掉队列；成功提交文章后才确认完成。`status` 会展示待处理数和 sitemap/补全检查异常，`status --strict` 可供无人值守监控。

HTTP 层包含：

- `ETag` / `If-None-Match`
- `Last-Modified` / `If-Modified-Since`
- 无 validator 时的响应体 SHA-256
- 25 秒超时、8 MB 响应上限
- 429/5xx 有界重试；长 `Retry-After` 写入跨轮次退避，首个 429 会停止该站本轮剩余详情请求
- 同一主机请求间隔限制
- 每个来源独立失败，不阻塞其他来源
- 解析条目数异常骤降时拒绝写入，防止网页改版污染状态

远端条目消失不会删除本地文章；文章标题或简介更新也会保留已读和收藏状态。

真正的新文章与通知待发送记录在同一数据库事务中提交。macOS 通知临时失败时，outbox 会在后续同步重试；即使通知不可用，文章仍保留为未读，不影响阅读页和 `packet`。

## 配置与扩展

来源配置在 `config/sources.json`。RSS 和 Atom 可以复用 `rss` adapter；网页来源应新增明确、可测试的固定 adapter，不建议在运行时用 LLM 临时猜 DOM。

运行时只用 Python 标准库。离线测试：

```bash
make test
```

fixture 测试会 mock 订阅源和 Provider 两类网络传输，不会联网、不会调用 LLM，也不会消耗 token。在线契约检查由 `doctor --live` 显式执行，用来验证四个主入口的解析契约并尽早发现上游网页结构变化；日常 `doctor` 只做本地检查。

## 设计边界

- 这里只保存发布方提供的标题、简介、类别和链接；默认不镜像全文。全文快照必须通过独立策略和命令显式启用。
- 不绕过登录、Cloudflare challenge 或 robots 限制。
- 内置 AI 增强默认关闭，永远不会由 `sync` 触发，必须显式启用配置/功能并执行用户命令或受保护的 loopback 点击。它强制版本化内容哈希缓存、有界输入输出、每日/每月硬预算和逐次调用审计。
- 阅读页默认只读；`read`、`unread`、`star` 和 `unstar` 会在修改后自动重新生成页面，也可以手动运行 `render`。消耗 token 的按钮只会出现在显式启用的 loopback 服务运行中。
