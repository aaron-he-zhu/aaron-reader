# Aaron Reader

[English](README.md)

**[在线站点](https://aaron-reader.aaron-he-zhu.workers.dev/)**

把 OpenAI 和 Anthropic 的官方文章收成一份列表，中英都能看。中文只翻译标题和简介，点进去读原文。

## 订阅源

- [OpenAI News](https://openai.com/news/rss.xml)
- [OpenAI Developer Blog](https://developers.openai.com/blog)
- [Claude Blog](https://claude.com/blog/)
- [Anthropic News](https://www.anthropic.com/news)

## 如何使用

**普通访客**：打开网站 → 点击 简 切换到中文 → 点击任意标题阅读原文。

**Aaron（或 Fork 运维者）**：站点通过 GitHub Actions 每天自动更新两次。如需手动触发更新，进入 Actions → 选择生产更新工作流 → Run workflow → 保留默认选项 → Run。

---

## 运维说明

以下内容为运维者和贡献者提供的技术文档。

### 云端架构

```text
官方发布方的 Feed 与网页
              │
              ▼
GitHub Actions —— America/Los_Angeles 每天 09:15 与 21:15
  1. 将公开爬虫状态和 AI 缓存恢复到临时数据库
  2. 抓取、解析、规范化 URL、计算指纹、去重并校验
  3. 仅把缺失或内容发生变化的语言任务交给 OpenRouter Free，必要时单向兜底到 DeepSeek
  4. 渲染并测试完整的中英文公开快照
  5. 只把严格限定的安全状态与快照文件提交到 main
              │
              ▼
Cloudflare Workers Builds
  构建已经验证的 GitHub 提交并发布只读站点
```

定时更新采用一条统一流水线，因此爬虫状态、AI 缓存与公开快照会一起推进。并发执行会被串行化，任何一轮任务都不能发布三者部分更新、彼此不一致的组合。

Cloudflare 不抓取发布方网页，也不调用模型。Worker 只提供已经通过 GitHub 工作流的结构、来源健康、隐私、lint、类型、构建和渲染结果校验的文件。

### 调度与 AI 更新频率

更新工作流每天在 **`America/Los_Angeles` 时区 09:15 与 21:15** 运行。这里使用命名时区而不是固定 UTC 偏移，因此 GitHub 会跟随旧金山夏令时变化。它们在太平洋夏令时分别对应北京时间 00:15 与 12:15，在太平洋标准时间分别对应北京时间 01:15 与 13:15，从而避开 DeepSeek 已公布的北京时间 09:00～12:00 和 14:00～18:00 高峰时段。

每轮定时任务都会：

- 用固定程序检查四个来源；
- 复用已经与当前文章内容哈希绑定的有效结果；
- 在有界的当前文章集合中扫描所有缺少简体中文译文的条目，而不只处理本轮新发现的文章，并按每轮配置上限依次补译；
- 只翻译标题和发布方摘要，并隔离单篇失败，使一个 `AIServiceError` 不会阻止后续缺译文章继续处理；
- 只发布通过严格校验的状态；AI 周期不完整时也会先发布安全的部分进度，再让 job 以警告结束。

除上述语言理解任务外，其他工作全部由固定程序完成，不消耗 LLM token：HTTP 缓存、解析、URL 规范化、文章身份、内容哈希、去重、来源健康检查、缓存选择、预算执行、序列化、渲染、测试、提交与部署准备。

### 固定 AI 提供方 Profile

生产任务默认使用封闭的 OpenRouter Free profile，并且只允许封闭的 DeepSeek V4 Flash profile 自动兜底。`config.ai.provider` 指定主 profile；也可以用 `ai cloud-run --provider ...` 仅覆盖当次任务。若人工把 DeepSeek 选为主提供方，该轮只使用 DeepSeek，绝不会反向兜底到 OpenRouter。

| Profile | 固定请求模型 | GitHub Actions Secret | 解析行为 |
| --- | --- | --- | --- |
| DeepSeek V4 Flash | `deepseek-v4-flash` | `DEEPSEEK_API_KEY` | DeepSeek 提供指定模型。 |
| OpenRouter Free | `openrouter/free` | `OPENROUTER_API_KEY` | OpenRouter 动态选择符合请求能力的免费模型，并在响应中返回实际模型。 |

提供方端点、请求模型、凭据环境变量名、关闭推理、结构化输出合约以及不授予模型工具权限等约束都由代码固定。系统会分别审计请求模型与实际解析模型；对动态 OpenRouter profile 来说，这一区分尤其重要。

Aaron Reader 应用层的自动兜底是单向且保守的。请求发出前，如果缺少 OpenRouter 凭据，可以直接切到 DeepSeek。请求发出后，只允许以下封闭条件触发兜底：OpenRouter 明确返回 `401`、`402`、`404` 或 `429`；完整用量已落账的终止错误 `rate_limit_exceeded`、`provider_overloaded` 或 `provider_unavailable`；以及完整用量已落账的封闭 profile 违规 `thinking_output`、`thinking_tokens` 或 `tool_calls`。本地结构化输出校验失败也必须满足"用量完整"这一条件。未知或未来新增的提供方错误码默认拒绝兜底。失败的 OpenRouter 请求与后续 DeepSeek 请求分别建立独立 job、attempt、幂等键、预算记录、请求模型和来源审计，绝不会伪装成一笔调用。

本轮第一次符合条件的 OpenRouter 失败会触发单向熔断：当前工作项只交给 DeepSeek 一次，之后本轮剩余工作继续使用 DeepSeek；不会出现第三次提供方调用，也不会从 DeepSeek 循环回 OpenRouter。下一轮定时任务会重新探测 OpenRouter。若进程中断或预算恰好卡在两次请求之间，持久化的 `fallback_pending` 冻结会让下一轮在公开交接文件成功导出后直接继续 DeepSeek，而不会重放 OpenRouter。

任何结果不明的情况都不会自动兜底或被定时任务自动重放。超时、连接中断、`408`／`409`／`425`、`5xx`、损坏、无类型、信号冲突或截断的响应、未知用量、已有生成冻结、预算不足、除前述"发送前缺少 OpenRouter 凭据"之外的其他本地配置或输入错误，以及 safety、moderation、content filter 或 policy 拒答，本身都不能授权跨提供方兜底。`400`、`403`、`422` 等明确但不可兜底的请求或政策失败会建立跨 profile 的 paid-failure 冻结。对于跨提供方续跑，`fallback_pending` 是唯一窄冻结例外：它只授权预先配置的 DeepSeek 续跑，绝不授权主提供方重放。

定时补译另有一条独立且更窄的 paid-failure 重放规则：对一篇缺译文章，每轮最多授权当前活动 provider profile 重放一次，并且只有当所有语义等价冻结都是 `paid_failure` 时才允许。只要任一等价冻结是 `ambiguous`，就绝不会自动重放。该重放消耗正常的每日请求数与 token 预算；若新的 OpenRouter 请求符合既有封闭兜底规则，仍可获得至多一次 OpenRouter→DeepSeek 续跑，但绝不会产生第三次调用。因此，一个持续明确失败的已计费工作项可以在后续每轮定时任务中各产生一次新的计费重放，直到成功、升级为 `ambiguous`，或被预算门禁挡住。如果本轮此前的文章已经让流程切到 DeepSeek，后续文章会继续使用该活动 profile，不会反向切回 OpenRouter。手动工作流默认不启用这条"全 paid-failure"窄策略。`force_held` 仍是显式的宽恢复开关，可以绕过 ambiguous 和 paid-failure 冻结，因此运维者必须确认可能重复计费的风险。仅更换 provider 不能绕过冻结。

每次真正发出 provider POST 前，本地 attempt 状态与 `ambiguous` 防重放冻结会在同一个 SQLite 事务中提交；明确响应会原子结算这份临时冻结。经过校验的文章翻译 artifact、用量与 attempt 完成状态也在同一事务中提交。只要工作流随后成功导出并发布 `cloud/ai-cache.json`，这些保护就能覆盖 Python／CLI 进程失败。但如果整台托管 runner 在请求到达提供方后、尚未导出本地数据库前彻底丢失，这并不是绝对 exactly-once 保证；系统也不假定提供方支持服务端幂等。生产工作流串行化为单写者，临时冻结的结算逻辑依赖这一边界。

`openrouter/free` 是动态的零费用路由目标，不是单一确定模型，也不是可靠性保证。它可选择的模型池、可用性、延迟、输出特征、上游提供方和上游数据处理政策都可能变化。OpenRouter 会收到请求，并可能在符合条件的上游提供方之间路由或故障切换；Aaron Reader 的单向规则只约束本应用另行发起的 OpenRouter→DeepSeek 续跑。因此 Aaron Reader 只发送有界的公开发布方元数据，不发送个人阅读状态或私密内容。启用前，运维者应检查 OpenRouter 当前的[免费模型路由说明](https://openrouter.ai/docs/guides/routing/routers/free-router)、[提供方路由说明](https://openrouter.ai/docs/guides/routing/provider-selection)与[提供方数据政策控制](https://openrouter.ai/docs/guides/privacy/provider-logging/)；绝不能用这套 profile 处理机密、个人或其他敏感输入。

### 持久化公开状态

GitHub 托管运行器是一次性的，因此仓库内保存两份刻意缩小且可以公开的续跑文件：

- `crawler/latest.json` 是严格的爬虫交接文件，保存来源身份、安全的文章元数据、内容指纹与有限的抓取续跑信息；它不包含 SQLite 数据库、个人已读／收藏状态、凭据或原始失败历史。
- `cloud/ai-cache.json` 保存经过校验的翻译，以及为兼容旧版本而保留的历史单篇摘要，并以稳定的发布方身份与内容哈希为键，而不是使用临时数据库行 ID。新的生产任务不再生成单篇摘要，公开阅读器投影也不会发布这些历史摘要。缓存还保存有界的旧金山时区聚合用量账本，以及不含输入内容的生成冻结指纹，让预算与防重放保护能够跨一次性 runner 延续。它不包含 API key、提供方请求 ID、逐请求审计、错误正文、prompt、模型响应、个人阅读状态或提取后的完整文章正文。

每轮任务开始时，两份文件都会先经过校验，再导入全新的临时 SQLite 数据库；结束时由固定序列化程序原子导出下一版公开状态。`site/data/` 与 `site/public/reader/` 中的部署数据也是公开投影；运行时数据库和临时文件不会进入 Git。

正式提交前，工作流会把刚导出的两份交接文件再次导入第二个空白验证数据库；网站只能从这个重建数据库渲染，并在发布前校验生成的公开快照。因此，只靠仓库里的公开状态就能在另一台一次性 runner 上重现部署站点。

由于 AI 结果与内容哈希绑定，同一篇未变化的文章在后续任务中会继续命中缓存。只要历史结果仍能通过当前校验，即使以后更换模型或提供方也可以复用；改变模型配置本身不会导致全部文章重新生成。

### 模型凭据

请把两套固定 profile 的凭据都配置为 GitHub Actions 仓库 Secret：

```text
DEEPSEEK_API_KEY
OPENROUTER_API_KEY
```

请在 **GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret** 中分别配置。密钥值只能填入 GitHub 的 Secret value 输入框，绝不能写入文件、工作流输入、Issue、提交、构建日志或 Cloudflare 变量。

工作流只会在有明确边界的 AI 生成步骤中提供凭据。默认 OpenRouter 路径会分别注入 `OPENROUTER_API_KEY` 与 `DEEPSEEK_API_KEY`，从而允许固定的单向兜底；人工选择 DeepSeek-only 时不会注入 OpenRouter 凭据。两把密钥不会合并成通用凭据，也不会接到另一提供方的端点。提供方端点和请求模型由代码固定；模型输出必须满足严格 JSON 合约并通过本地校验；模型没有任何工具权限。浏览器、Cloudflare Worker、公开快照、Pull Request 检查和确定性爬虫都拿不到任一密钥。

Fork 不会继承原仓库的 Secret。每个 Fork 都必须自行添加 `DEEPSEEK_API_KEY` 与 `OPENROUTER_API_KEY`，生产更新工作流才能使用全部两套 profile。

### Token、预算与失败处理

Aaron Reader 从多个层面减少计费任务：

- 每轮定时任务对每篇缺译文章最多发起一次初始 provider 请求或窄授权的 paid-failure 重放，并且只翻译标题和发布方摘要；
- 内容哈希完全匹配时跳过模型，包括仍满足当前校验的历史结果；
- 有界聚合账本会把已确认用量和未知结果的保守预留带到下一台 runner，因此每天／每月的请求数与 token 上限不会随一次性任务重置；
- 配置对文章数量、输入字符、输出 token、响应大小、请求数量、总 token、超时和工作器并发设置硬上限；
- 两套固定 profile 都会为这类结构化转换任务关闭推理；
- 每次真实的 OpenRouter 或 DeepSeek 请求都有独立审计，并消耗共享的请求数与 token 预算；
- 符合兜底条件的 OpenRouter 失败最多增加一次 DeepSeek 请求，并在运行结果中明确标记为降级；
- 结果不明、用量未知或安全／政策拒答绝不会兜底，并会建立适当的稳定生成冻结；定时自动重放仅限所有等价冻结均为 `paid_failure` 的工作项。

来源健康、状态格式、隐私、测试、仓库边界或站点构建失败时，流水线会直接停止并且不发布任何内容。文章级 AI 失败彼此隔离：一个 `AIServiceError` 会被记录，但定时扫描仍继续处理后续缺译文章。为了避免同一份成功结果被重复计费，AI 周期未完整完成时采用更精细的处理：无效的模型输出会被丢弃，但此前已经通过严格校验的结果、聚合用量更新和生成冻结会先导出并发布，然后 job 以警告结束。因此，凭据缺失、提供方错误、冻结或预算耗尽绝不会发布未经校验的内容，而已安全完成的部分进度可以在下一轮继续复用。

### 手动运行一次更新

打开仓库的 **Actions** 页面，在 `.github/workflows/` 下选择生产更新工作流，点击 **Run workflow**，保留默认的 `openrouter` 主提供方，或选择 `deepseek` 执行仅 DeepSeek 的诊断任务，然后在 `main` 上运行。手动任务与定时任务使用相同的临时数据库、Secret 边界、缓存规则、安全上限、测试、精确文件提交和 Cloudflare 发布路径，但手动任务默认不启用定时任务的"全 paid-failure"窄重放策略。`force_held` 是显式的宽恢复开关，可能重复一笔已经计费或结果不明的生成；除非已经查看工作流 Summary 并明确接受这项风险，否则不要启用。定时任务绝不会启用 `force_held`；它们独立的自动授权只允许对所有等价冻结均为 `paid_failure` 的文章每轮在当前活动 profile 上重放一次。

工作流 Summary 是运行记录：它会报告缓存命中、提供方调用与 token 用量、失败项、发生变化的公开文件以及是否提交了新版本；Secret 值和提供方响应正文不会写入日志。

### 部署自己的 Fork

如果要托管独立实例：

1. Fork 仓库，并允许生产工作流写入仓库内容。
2. 为 Fork 需要使用的每套 profile 添加对应的 GitHub Actions 仓库 Secret：`DEEPSEEK_API_KEY`、`OPENROUTER_API_KEY`，或两者都添加。
3. 将 Fork 连接到 Cloudflare Workers Builds，生产分支设为 `main`，应用根目录设为 `site/`。
4. 保留仓库中的 lockfile 与 `site/wrangler.jsonc`；Cloudflare 不需要模型 Secret 或 AI binding。
5. 添加名为 `PUBLIC_SITE_URL` 的 GitHub Actions 仓库变量，值为 Fork 不含凭据的 HTTPS 公开域名。
6. 如需指定定时任务的 profile，把可选的 GitHub Actions 仓库变量 `AI_PROVIDER` 设为 `openrouter` 或 `deepseek`；未配置时默认使用 `openrouter`，并启用固定的 DeepSeek 兜底。
7. 启用定时生产工作流，并先手动运行一次，验证完整路径。

工作流会从 GitHub 不可变的当前仓库上下文派生写入边界，并在 push 前核对 checkout origin，因此 Fork 可以运行，同时拿不到上游仓库的写权限。Cloudflare 应只部署 `main` 上的提交；仓库中的 Worker 配置有意关闭预览 URL。

每次 push 后，工作流都会轮询 `PUBLIC_SITE_URL`，并把线上 reader 快照与已验证提交逐字节比较；只有 Cloudflare 已经发布完全一致的状态，任务才会成功。这项检查只访问公开站点，不需要 Cloudflare 凭据。

### 开发与验证

开发和 CI 检查可在任意临时 checkout 中执行；任何 checkout、个人电脑进程或开发数据库都不是生产运行时的一部分。

开发环境要求：

- 确定性阅读器和测试使用 Python 3.9 或更高版本；
- Cloudflare 站点使用 Node.js 22.13 或更高版本；
- 测试、确定性同步、渲染和站点构建不需要 API key。

在仓库根目录运行 Python 测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

运行网页检查：

```bash
cd site
npm ci --ignore-scripts --no-audit --no-fund
npm run lint
npm run typecheck
npm test
```

如果只想用固定程序在线检查解析器合约而不保存文章：

```bash
./aaron-reader doctor --live
```

测试不得调用真实模型、GitHub 或 Cloudflare API。贡献规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题报告方式见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
