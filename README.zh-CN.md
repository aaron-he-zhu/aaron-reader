# Aaron Reader

[English](README.md)

[Cloudflare 线上站点](https://aaron-reader.aaron-he-zhu.workers.dev/) · [GitHub 开源仓库](https://github.com/aaron-he-zhu/aaron-reader)

Aaron Reader 是一个订阅 OpenAI 与 Anthropic 官方内容的双语云端阅读器。GitHub Actions 负责收集、校验新文章；DeepSeek 只负责必须依赖语言理解的摘要、简体中文翻译与简报；Cloudflare Workers 提供最终的只读网页。

生产环境完全运行在 GitHub 和 Cloudflare 上，不依赖个人电脑、本地数据库、桌面客户端或交互式 AI 订阅。

## 订阅源

- [OpenAI News](https://openai.com/news/rss.xml)
- [OpenAI Developer Blog](https://developers.openai.com/blog)
- [Claude Blog](https://claude.com/blog/)
- [Anthropic News](https://www.anthropic.com/news)

英文是默认界面；语言选择器提供完整的简体中文界面，并在存在中文缓存时自动显示译文。网页上没有“翻译”或“摘要”按钮，因为这些内容由云端任务自动补齐。

## 云端架构

```text
官方发布方的 Feed 与网页
              │
              ▼
GitHub Actions —— America/Los_Angeles 每天 10:00 与 22:00
  1. 将公开爬虫状态和 AI 缓存恢复到临时数据库
  2. 抓取、解析、规范化 URL、计算指纹、去重并校验
  3. 仅把缺失或正文发生变化的语言任务交给 DeepSeek
  4. 渲染并测试完整的中英文公开快照
  5. 只把严格限定的安全状态与快照文件提交到 main
              │
              ▼
Cloudflare Workers Builds
  构建已经验证的 GitHub 提交并发布只读站点
```

定时更新采用一条统一流水线，因此爬虫状态、AI 缓存与公开快照会一起推进。并发执行会被串行化，任何一轮任务都不能发布三者部分更新、彼此不一致的组合。

Cloudflare 不抓取发布方网页，也不调用模型。Worker 只提供已经通过 GitHub 工作流的结构、来源健康、隐私、lint、类型、构建和渲染结果校验的文件。

## 调度与 AI 更新频率

更新工作流每天在 **`America/Los_Angeles` 时区 10:00 与 22:00** 运行。这里使用命名时区而不是固定 UTC 偏移，因此 GitHub 会跟随旧金山夏令时变化。

每轮成功任务都会：

- 用固定程序检查四个来源；
- 复用已经与当前文章内容哈希绑定的有效结果；
- 使用同一个共享元数据 `deepseek-v4-flash` 请求同时补齐每篇缺失的简体中文摘要与翻译；
- 检查中英文日报；两种语言都缺失时用一个共享文章时间窗一次生成，且只有经过校验的输入发生变化时才调用模型；
- 所有必要检查通过后才发布新快照。

周报与日报的定位不同：周报覆盖旧金山日历周，对多个来源的长期主题进行综合，并且**只在旧金山时间周日晚上生成一次**，不会在每天两轮任务中反复重做。手动触发的更新也遵循同一套缓存和校验规则。

除上述语言理解任务外，其他工作全部由固定程序完成，不消耗 LLM token：HTTP 缓存、解析、URL 规范化、文章身份、内容哈希、去重、来源健康检查、缓存选择、预算执行、序列化、渲染、测试、提交与部署准备。

## 持久化公开状态

GitHub 托管运行器是一次性的，因此仓库内保存两份刻意缩小且可以公开的续跑文件：

- `crawler/latest.json` 是严格的爬虫交接文件，保存来源身份、安全的文章元数据、内容指纹与有限的抓取续跑信息；它不包含 SQLite 数据库、个人已读／收藏状态、凭据或原始失败历史。
- `cloud/ai-cache.json` 保存经过校验的摘要、翻译和日／周报，以稳定的发布方身份与内容哈希为键，而不是使用临时数据库行 ID；它还保存有界的旧金山时区聚合用量账本，以及不含输入内容的生成冻结指纹，让预算与防重放保护能够跨一次性 runner 延续。它不包含 API key、提供方请求 ID、逐请求审计、错误正文、prompt、模型响应、个人阅读状态或提取后的完整文章正文。

每轮任务开始时，两份文件都会先经过校验，再导入全新的临时 SQLite 数据库；结束时由固定序列化程序原子导出下一版公开状态。`site/data/` 与 `site/public/reader/` 中的部署数据也是公开投影；运行时数据库和临时文件不会进入 Git。

由于 AI 结果与内容哈希绑定，同一篇未变化的文章在后续任务中会继续命中缓存。只要历史结果仍能通过当前校验，即使以后更换模型或提供方也可以复用；改变模型配置本身不会导致全部文章重新生成。

## DeepSeek 密钥

生产环境唯一的模型凭据是名为下面这个名称的 GitHub Actions 仓库 Secret：

```text
DEEPSEEK_API_KEY
```

请在 **GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret** 中配置。密钥值只能填入 GitHub 的 Secret value 输入框，绝不能写入文件、工作流输入、Issue、提交、构建日志或 Cloudflare 变量。

工作流只在有明确边界的 DeepSeek 步骤中暴露这个 Secret。提供方端点和模型由代码固定；模型输出必须满足严格 JSON 合约并通过本地校验；模型没有任何工具权限。浏览器、Cloudflare Worker、公开快照、Pull Request 检查和确定性爬虫都拿不到密钥。

Fork 不会继承原仓库的 Secret。每个 Fork 都必须自行添加 `DEEPSEEK_API_KEY`，它的生产更新工作流才能执行 AI 补全。

## Token、预算与失败处理

Aaron Reader 从多个层面减少计费任务：

- 新文章同时缺少摘要和中文翻译时，会在同一请求中获取两项结果；
- 中英文报告在两种语言都缺失时共享一次文章时间窗请求，把正常日报从两次调用降为一次，并把周日“日报＋周报”的四次调用降为两次；
- 内容哈希完全匹配时跳过模型，包括仍满足当前校验的历史结果；
- 报告输入哈希会阻止未变化的日／周时间窗重复生成；
- 有界聚合账本会把已确认用量和未知结果的保守预留带到下一台 runner，因此每天／每月的请求数与 token 上限不会随一次性任务重置；
- 配置对文章数量、输入字符、输出 token、响应大小、请求数量、总 token、超时和工作器并发设置硬上限；
- 这类结构化转换任务关闭 DeepSeek 推理；
- 如果网络错误无法确认请求是否已经计费，或已计费结果未通过校验，系统会建立稳定的生成冻结；后续定时任务会零调用跳过这项准确工作，并让 GitHub 工作流保持可见的失败告警。

来源健康、状态格式、隐私、测试、仓库边界或站点构建失败时，流水线会直接停止并且不发布任何内容。为了避免同一份成功结果被重复计费，AI 周期未完整完成时采用更精细的处理：无效的模型输出会被丢弃，但此前已经通过严格校验的结果、聚合用量更新和生成冻结会先导出并发布，然后工作流再以失败状态报警。因此，凭据缺失、提供方错误或预算耗尽绝不会发布未经校验的内容，而已安全完成的部分进度可以在下一轮继续复用。

## 手动运行一次更新

打开仓库的 **Actions** 页面，在 `.github/workflows/` 下选择生产更新工作流，点击 **Run workflow**，并在 `main` 上运行。手动任务与定时任务使用完全相同的临时数据库、Secret 边界、缓存规则、安全上限、测试、精确文件提交和 Cloudflare 发布路径。`force_weekly` 是一次性的强制周报开关；`force_held` 是另一项恢复开关，它可能重复一笔已经计费的生成。除非已经查看工作流 Summary 并明确接受这笔费用，否则不要启用 `force_held`。定时任务不会启用这两个开关。

工作流 Summary 是运行记录：它会报告缓存命中、提供方调用与 token 用量、失败项、发生变化的公开文件以及是否提交了新版本；Secret 值和提供方响应正文不会写入日志。

## 部署自己的 Fork

如果要托管独立实例：

1. Fork 仓库，并允许生产工作流写入仓库内容。
2. 添加名为 `DEEPSEEK_API_KEY` 的 GitHub Actions 仓库 Secret。
3. 将 Fork 连接到 Cloudflare Workers Builds，生产分支设为 `main`，应用根目录设为 `site/`。
4. 保留仓库中的 lockfile 与 `site/wrangler.jsonc`；Cloudflare 不需要模型 Secret 或 AI binding。
5. 添加名为 `PUBLIC_SITE_URL` 的 GitHub Actions 仓库变量，值为 Fork 不含凭据的 HTTPS 公开域名。
6. 启用定时生产工作流，并先手动运行一次，验证完整路径。

工作流会从 GitHub 不可变的当前仓库上下文派生写入边界，并在 push 前核对 checkout origin，因此 Fork 可以运行，同时拿不到上游仓库的写权限。Cloudflare 应只部署 `main` 上的提交；仓库中的 Worker 配置有意关闭预览 URL。

每次 push 后，工作流都会轮询 `PUBLIC_SITE_URL`，并把线上 reader 快照与已验证提交逐字节比较；只有 Cloudflare 已经发布完全一致的状态，任务才会成功。这项检查只访问公开站点，不需要 Cloudflare 凭据。

## 开发与验证

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
