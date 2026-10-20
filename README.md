# GitHub 开源看板

追踪 GitHub 开源项目的 star 增量：今日、本周、本月的飙升项目、新秀和我的关注。
每个项目由 Claude 读 README 生成中文介绍和能力卡片，可以按场景类目、年轻人痛点筛选，按可串联指数排序。
零服务器：GitHub Actions 定时抓取 → JSON 快照提交回仓库 → GitHub Pages 托管静态页面。

## 目录

```
index.html                    看板页面（纯静态）
config/taxonomy.json          类目、痛点和能力卡片的取值表（抓取、生成、前端共用）
scripts/fetch.py              抓取脚本（只用 Python 标准库）
scripts/enrich.py             用 Claude 生成中文介绍和能力卡片
.github/workflows/update.yml  每 6 小时运行一次抓取 + 生成并提交数据
data/latest.json              前端数据：元信息 + d1/d7/d30 增量 + 30 天迷你曲线
data/enriched.json            每个仓库的中文介绍和能力卡片
data/history.json             每日 star 快照（保留 90 天）
data/tracked.json             跟踪池：每个仓库首次、最近一次出现在来源中的日期
data/state.json               杂项状态（比如今天是否已做过类目搜索）
```

## 中文介绍和能力卡片

需要在仓库 **Settings → Secrets and variables → Actions** 里添加 Secret `ANTHROPIC_API_KEY`。没配置时这一步会自动跳过，看板照常更新，只是没有中文介绍。

- 默认模型是 `claude-haiku-5-5`，成本低。想换模型，在同一页的 **Variables** 里加 `ENRICH_MODEL`（比如 `claude-sonnet-5-5`、`claude-opus-5-5`）。
- 每次运行最多处理 120 个仓库（环境变量 `ENRICH_MAX_PER_RUN`），按周增量从高到低排队；处理过的会缓存，满 60 天且有新提交才重做。
- README 只取前 12000 个字符发给模型。
- 本地查看会发给模型的内容：`python3 scripts/enrich.py --dry-run owner/repo`

卡片字段（取值见 `config/taxonomy.json`）：

| 分组 | 字段 |
| --- | --- |
| 中文介绍 | `summary_zh` 一句话简介、`features_zh` 能做什么、`use_cases_zh` 使用场景、`hook_zh` 面向年轻人的卖点、`audience` |
| 分类 | `categories`（1 到 3 个，含二级类目和置信度）、`pain_points`（最多 2 个） |
| 串联能力 | `inputs`、`outputs`、`interfaces`、`batch_ok` |
| 运行条件 | `deploy`、`requires`、`difficulty`（1 到 3）、`platforms`、`cost`、`newbie_friendly` |
| 其他 | `demo_url`、`has_media`、`risk_note` |

前端再结合抓取到的许可证和最后提交时间，算出两项指标：
- **可串联指数**（0 到 100）：调用方式 30 + 有标准格式输出 15 + 能批量运行 15 + 许可证 15 + 维护活跃 15 + 上手难度 10。
- **适合包成 Skill**：能批量运行，有 CLI、库或 HTTP API，并且不需要机器人硬件。

增删类目或痛点：直接改 `config/taxonomy.json`。新加的类目会从下一次抓取开始按它的 `search` 关键词补充跟踪池；已经生成过卡片的仓库不会自动重新分类，需要的话删掉 `data/enriched.json` 里对应的条目。

## 部署

1. 新建一个 GitHub 仓库，把这些文件推上去。
2. **Settings → Actions → General → Workflow permissions** 选 *Read and write permissions*。
3. **Settings → Pages**：Source 选 *Deploy from a branch*，分支选 `main`，目录选 `/ (root)`。
4. **Actions → Update data → Run workflow** 手动跑一次。之后每 6 小时自动运行。
5. （可选）跟踪的仓库超过约 3000 个，或者内置 token 被限流时，创建一个不带任何权限的 fine-grained PAT，存进仓库 Secret `GH_PAT`。

## 本地运行

```bash
GITHUB_TOKEN=$(gh auth token) python3 scripts/fetch.py
python3 -m http.server 8765    # 然后打开 http://localhost:8765
```

不带 token 也能跑，但只会刷新 Trending 和 Search 返回的仓库。

## 数据是怎么来的

| 来源 | 用途 |
| --- | --- |
| `github.com/trending`（全部语言 + 若干常用语言，按日/周/月） | 发现热门仓库；历史快照不够时，用它给出的数字估算增量（页面上带 `~`） |
| Search API：`created:>30天前 stars:>20` 按 star 排序 | 发现「新秀」 |
| Search API：每个类目的 `search` 关键词，半年内有更新、star > 100（每天一次，需要 token） | 补充 Trending 很少覆盖的类目，比如求职、SEO、电商 |
| GraphQL API（每批 80 个仓库） | 每次运行刷新整个跟踪池的 star、fork、语言、topics 等 |

增量 = 当前 star − N 天前那次快照的 star。某天的快照缺了，会往前多找 1 天（月增量多找 5 天）；还找不到就不显示，不拿更旧的数据凑数。
仓库连续 45 天没出现在任何来源里、并且周增量低于 50，就移出跟踪池。

**快照满 7 天后，周榜才全部是实测值；满 30 天后月榜才是。所以先把 Actions 跑起来。**

## 可调参数（`scripts/fetch.py` 顶部）

- `TRENDING_LANGS`：额外抓取哪些语言的 Trending（也可以用同名环境变量覆盖，逗号分隔，空项代表全部语言）
- `MAX_TRACKED` / `STALE_DAYS` / `HISTORY_DAYS`：跟踪池大小、移出规则、快照保留天数

## 下一步可以做

- 流水线模板页：按场景预设步骤（比如自媒体视频：下载 → 字幕 → 翻译 → 切片 → 封面 → 发布），每一步用能力卡片的输入输出自动匹配候选项目
- 仓库详情页：完整 star 曲线、贡献者、release 节奏
- 组合助手：勾选几个项目，让模型写串联方案（需要一个小后端转发 API 请求）
- 每周各类目新工具中文周报，推送到 RSS、Telegram 或邮件
