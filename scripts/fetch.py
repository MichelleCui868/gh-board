#!/usr/bin/env python3
"""抓取 GitHub 热门仓库，记录每日 star 快照，生成看板数据。

只依赖标准库。用法：
    GITHUB_TOKEN=xxx python3 scripts/fetch.py

输出：
    data/history.json  每日 star 快照（按仓库对齐日期，最多保留 HISTORY_DAYS 天）
    data/tracked.json  跟踪池（每个仓库首次/最近一次出现在种子来源的日期）
    data/latest.json   前端读取的看板数据（元信息 + 增量 + 迷你曲线）

没有 GITHUB_TOKEN 时也能跑：只刷新 Trending/Search 拿到的仓库，其余仓库当天记为空值。
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
# 额外抓取这些语言的 Trending（空字符串 = 全部语言）
TRENDING_LANGS = [s.strip() for s in os.environ.get(
    "TRENDING_LANGS", ",python,typescript,javascript,go,rust,java,c++").split(",")]
TRENDING_PERIODS = ["daily", "weekly", "monthly"]
NEW_REPO_DAYS = 30          # 「新秀」= 最近 N 天内创建
SEARCH_PAGES = 3            # 新秀搜索每页 100 个
MAX_TRACKED = 3000          # 跟踪池上限
STALE_DAYS = 45             # 超过 N 天没出现在任何来源、且周增量很低的仓库会被移出
HISTORY_DAYS = 90
SPARK_DAYS = 30
UA = "gh-board/0.1 (+https://github.com)"


# ---------- HTTP ----------

def request(url, data=None, headers=None, retries=3):
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After") or 20 * (attempt + 1))
                log("限流 %s，%d 秒后重试" % (e.code, wait))
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(3)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(3)
                continue
            raise


def api_headers():
    h = {"Accept": "application/vnd.github+json"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    return h


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def to_int(s):
    s = (s or "").replace(",", "").strip()
    return int(s) if s.isdigit() else 0


# ---------- 种子来源 ----------

ROW_RE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
NAME_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([^"/]+/[^"/]+)"', re.S)
DESC_RE = re.compile(r'<p class="col-9[^"]*">(.*?)</p>', re.S)
LANG_RE = re.compile(r'<span itemprop="programmingLanguage">(.*?)</span>')
COLOR_RE = re.compile(r'repo-language-color" style="background-color: (#[0-9a-fA-F]+)"')
STARS_RE = re.compile(r'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', re.S)
FORKS_RE = re.compile(r'href="/[^"]+/forks"[^>]*>(.*?)</a>', re.S)
PERIOD_RE = re.compile(r'([\d,]+) stars? (today|this week|this month)')


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def fetch_trending(period, lang=""):
    path = "/trending/" + urllib.parse.quote(lang) if lang else "/trending"
    url = "https://github.com%s?since=%s" % (path, period)
    page = request(url, headers={"User-Agent": "Mozilla/5.0"})
    out = []
    for row in ROW_RE.findall(page):
        m = NAME_RE.search(row)
        if not m:
            continue
        p = PERIOD_RE.search(row)
        lang_m, color_m = LANG_RE.search(row), COLOR_RE.search(row)
        out.append({
            "name": m.group(1),
            "description": strip_tags(DESC_RE.search(row).group(1)) if DESC_RE.search(row) else "",
            "language": strip_tags(lang_m.group(1)) if lang_m else None,
            "lang_color": color_m.group(1) if color_m else None,
            "stars": to_int(strip_tags(STARS_RE.search(row).group(1))) if STARS_RE.search(row) else 0,
            "forks": to_int(strip_tags(FORKS_RE.search(row).group(1))) if FORKS_RE.search(row) else 0,
            "period_stars": to_int(p.group(1)) if p else 0,
        })
    return out


def from_rest(r):
    return {
        "name": r["full_name"],
        "description": r.get("description") or "",
        "language": r.get("language"),
        "stars": r.get("stargazers_count", 0),
        "forks": r.get("forks_count", 0),
        "topics": (r.get("topics") or [])[:6],
        "created_at": r.get("created_at"),
        "pushed_at": r.get("pushed_at"),
        "homepage": r.get("homepage") or "",
        "archived": r.get("archived", False),
    }


def search_new_repos(today):
    since = (today - dt.timedelta(days=NEW_REPO_DAYS)).isoformat()
    q = "created:>%s stars:>20" % since
    out = []
    for page in range(1, SEARCH_PAGES + 1):
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
        items = json.loads(request(url, headers=api_headers())).get("items", [])
        out.extend(from_rest(r) for r in items)
        if len(items) < 100:
            break
        if not TOKEN:
            time.sleep(7)  # 未认证搜索限 10 次/分钟
    return out


# ---------- GraphQL 批量刷新 ----------

GQL_FIELDS = """
  nameWithOwner description url homepageUrl stargazerCount forkCount
  createdAt pushedAt isArchived
  primaryLanguage { name color }
  repositoryTopics(first: 6) { nodes { topic { name } } }
"""


def refresh_graphql(names):
    """返回 {请求时的 name: 元信息 或 None(已删除/不可见)}"""
    result = {}
    names = list(names)
    for i in range(0, len(names), 80):
        chunk = names[i:i + 80]
        parts = []
        for j, name in enumerate(chunk):
            owner, repo = name.split("/", 1)
            parts.append('r%d: repository(owner: %s, name: %s) { %s }'
                         % (j, json.dumps(owner), json.dumps(repo), GQL_FIELDS))
        query = "query {\n%s\n}" % "\n".join(parts)
        try:
            resp = json.loads(request("https://api.github.com/graphql",
                                      data={"query": query}, headers=api_headers()))
        except urllib.error.HTTPError as e:
            log("GraphQL 批次 %d 失败：%s" % (i // 80, e))
            continue
        data = resp.get("data") or {}
        for j, name in enumerate(chunk):
            r = data.get("r%d" % j)
            if r is None:
                result[name] = None
                continue
            lang = r.get("primaryLanguage") or {}
            result[name] = {
                "name": r["nameWithOwner"],
                "description": r.get("description") or "",
                "language": lang.get("name"),
                "lang_color": lang.get("color"),
                "stars": r["stargazerCount"],
                "forks": r["forkCount"],
                "topics": [n["topic"]["name"] for n in r["repositoryTopics"]["nodes"]],
                "created_at": r["createdAt"],
                "pushed_at": r["pushedAt"],
                "homepage": r.get("homepageUrl") or "",
                "archived": r["isArchived"],
            }
        log("GraphQL 已刷新 %d/%d" % (min(i + 80, len(names)), len(names)))
    return result


# ---------- 存储 ----------

def load(name, default):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(name, obj, pretty=False):
    path = os.path.join(DATA, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def delta_since(dates, series, now_stars, today, days):
    """当前 star 减去 days 天前快照的 star。

    那天没有快照时，往前找最近一次，但最多多退 1/1/5 天（d1/d7/d30），
    避免缺几天数据后把「今日」算成一周的增量。找不到返回 None。
    """
    target = (today - dt.timedelta(days=days)).isoformat()
    oldest = (today - dt.timedelta(days=days + max(1, days // 6))).isoformat()
    for d, v in zip(reversed(dates), reversed(series)):
        if d < oldest:
            break
        if d <= target and v is not None:
            return now_stars - v
    return None


def main():
    os.makedirs(DATA, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    today_s = today.isoformat()
    log("开始抓取 %s，%s token" % (today_s, "有" if TOKEN else "无"))

    history = load("history.json", {"dates": [], "stars": {}})
    tracked = load("tracked.json", {})
    prev_meta = {r["name"]: r for r in load("latest.json", {"repos": []})["repos"]}

    # 1. 种子：Trending + 新秀搜索
    seeds = {}            # name -> 元信息（来自种子）
    trending = {p: {} for p in TRENDING_PERIODS}   # period -> {name: period_stars}
    trending_rank = {}    # 当天全语言 daily 榜排名
    for lang in TRENDING_LANGS:
        for period in TRENDING_PERIODS:
            try:
                rows = fetch_trending(period, lang)
            except Exception as e:  # noqa: BLE001 单个来源失败不影响整体
                log("Trending %s/%s 失败：%s" % (lang or "all", period, e))
                continue
            for idx, r in enumerate(rows):
                trending[period][r["name"]] = max(trending[period].get(r["name"], 0), r["period_stars"])
                if period == "daily" and not lang:
                    trending_rank[r["name"]] = idx + 1
                seeds.setdefault(r["name"], {}).update({k: v for k, v in r.items() if k != "period_stars"})
            time.sleep(1)
    log("Trending 共 %d 个仓库" % len(seeds))
    try:
        for r in search_new_repos(today):
            seeds.setdefault(r["name"], {}).update(r)
    except Exception as e:  # noqa: BLE001
        log("新秀搜索失败：%s" % e)
    log("种子合计 %d 个仓库" % len(seeds))

    for name in seeds:
        t = tracked.setdefault(name, {"first_seen": today_s})
        t["last_seen"] = today_s

    # 2. 刷新元信息
    meta = {}
    if TOKEN:
        refreshed = refresh_graphql(tracked.keys())
        for name, r in refreshed.items():
            if r is None:
                log("移除不可访问仓库 %s" % name)
                tracked.pop(name, None)
                history["stars"].pop(name, None)
                continue
            if r["name"] != name:   # 仓库改名/转移，迁移历史
                tracked[r["name"]] = tracked.pop(name)
                if name in history["stars"]:
                    history["stars"][r["name"]] = history["stars"].pop(name)
            meta[r["name"]] = r
    for name, r in seeds.items():   # 种子数据补齐（无 token 时这是唯一的新数据）
        if name not in meta:
            merged = dict(prev_meta.get(name, {}))
            merged.update(r)
            meta[name] = merged
    fresh = set(meta)
    for name in tracked:            # 没刷新到的沿用旧元信息，但不写入今天的快照
        if name not in meta and name in prev_meta:
            meta[name] = prev_meta[name]

    # 3. 写入今日快照
    dates = history["dates"]
    if not dates or dates[-1] != today_s:
        dates.append(today_s)
        for s in history["stars"].values():
            s.append(None)
    n = len(dates)
    for name in tracked:
        s = history["stars"].setdefault(name, [None] * n)
        if len(s) < n:
            s[:0] = [None] * (n - len(s))
        if name in fresh:
            s[-1] = meta[name]["stars"]
    if n > HISTORY_DAYS:
        cut = n - HISTORY_DAYS
        history["dates"] = dates = dates[cut:]
        for k in history["stars"]:
            history["stars"][k] = history["stars"][k][cut:]

    # 4. 计算增量，生成看板数据
    new_cutoff = (today - dt.timedelta(days=NEW_REPO_DAYS)).isoformat()
    repos = []
    for name in tracked:
        m = meta.get(name)
        if not m:
            continue
        series = history["stars"].get(name, [])
        stars = m["stars"]
        created = (m.get("created_at") or "")[:10]
        row = dict(m)
        for key, days, period in (("d1", 1, "daily"), ("d7", 7, "weekly"), ("d30", 30, "monthly")):
            v = delta_since(dates, series, stars, today, days)
            est = False
            if v is None and created and created >= (today - dt.timedelta(days=days)).isoformat():
                v = stars                       # 仓库比统计窗口还新：全部 star 都是窗口内涨的
            if v is None and name in trending[period]:
                v, est = trending[period][name], True   # 历史不够时用 Trending 给出的数字
            row[key] = v
            if est:
                row[key + "_est"] = True
        row["is_new"] = bool(created and created >= new_cutoff)
        if name in trending_rank:
            row["trending_rank"] = trending_rank[name]
        row["spark"] = series[-SPARK_DAYS:]
        repos.append(row)

    # 5. 清理跟踪池：长期没出现在来源里、且周增量很低的仓库移出；超过上限按周增量截断
    stale_cutoff = (today - dt.timedelta(days=STALE_DAYS)).isoformat()
    keep = []
    for r in repos:
        t = tracked[r["name"]]
        if t.get("last_seen", "") < stale_cutoff and (r.get("d7") or 0) < 50:
            tracked.pop(r["name"])
            history["stars"].pop(r["name"], None)
        else:
            keep.append(r)
    keep.sort(key=lambda r: (r.get("d7") or 0, r.get("d1") or 0, r["stars"]), reverse=True)
    for r in keep[MAX_TRACKED:]:
        tracked.pop(r["name"], None)
        history["stars"].pop(r["name"], None)
    repos = keep[:MAX_TRACKED]

    save("history.json", history)
    save("tracked.json", tracked, pretty=True)
    save("latest.json", {
        "generated_at": now.replace(microsecond=0).isoformat(),
        "history_days": len(dates),
        "spark_dates": dates[-SPARK_DAYS:],
        "repos": repos,
    })
    log("完成：跟踪 %d 个仓库，今日刷新 %d 个，历史 %d 天" % (len(repos), len(fresh), len(dates)))


if __name__ == "__main__":
    main()
