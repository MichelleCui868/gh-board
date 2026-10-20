# GitHub 开源看板

追踪 GitHub 开源项目的 star 增量：今日、本周、本月的飙升项目、新秀和我的关注。
零服务器：GitHub Actions 定时抓取 → JSON 快照提交回仓库 → GitHub Pages 托管静态页面。

## 目录

```
index.html                    看板页面（纯静态，读取 data/latest.json）
scripts/fetch.py              抓取脚本（只用 Python 标准库）
.github/workflows/update.yml  每 6 小时运行一次抓取并提交数据
data/latest.json              前端数据：元信息 + d1/d7/d30 增量 + 30 天迷你曲线
data/history.json             每日 star 快照（保留 90 天）
data/tracked.json             跟踪池：每个仓库首次、最近一次出现在来源中的日期
```

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
| GraphQL API（每批 80 个仓库） | 每次运行刷新整个跟踪池的 star、fork、语言、topics 等 |

增量 = 当前 star − N 天前那次快照的 star。某天的快照缺了，会往前多找 1 天（月增量多找 5 天）；还找不到就不显示，不拿更旧的数据凑数。
仓库连续 45 天没出现在任何来源里、并且周增量低于 50，就移出跟踪池。

**快照满 7 天后，周榜才全部是实测值；满 30 天后月榜才是。所以先把 Actions 跑起来。**

## 可调参数（`scripts/fetch.py` 顶部）

- `TRENDING_LANGS`：额外抓取哪些语言的 Trending（也可以用同名环境变量覆盖，逗号分隔，空项代表全部语言）
- `MAX_TRACKED` / `STALE_DAYS` / `HISTORY_DAYS`：跟踪池大小、移出规则、快照保留天数

## 下一步可以做

- 用 LLM 读 README，生成一句话中文简介和领域分类（AI/Agent/MCP/前端…），每天只处理新出现的仓库
- 生成每日简报，推送到 RSS、Telegram 或邮件
- 仓库详情页：完整 star 曲线、贡献者、release 节奏
