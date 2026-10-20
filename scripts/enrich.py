#!/usr/bin/env python3
"""用 Claude 读 README，给跟踪池里的仓库生成中文介绍和能力卡片。

在 fetch.py 之后运行。每个仓库只处理一次（满 REENRICH_DAYS 天且有新提交才重做），
每次最多处理 MAX_PER_RUN 个，按周增量从高到低排队。

用法：
    ANTHROPIC_API_KEY=xxx GITHUB_TOKEN=xxx python3 scripts/enrich.py
    python3 scripts/enrich.py --dry-run owner/repo   # 只打印发给模型的内容，不调用 API

输出：data/enriched.json  {仓库名: 能力卡片}
取值表在 config/taxonomy.json，前端也读这份。
"""
import base64
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import ROOT, api_headers, load, log, request, save  # noqa: E402

MODEL = os.environ.get("ENRICH_MODEL", "claude-haiku-5-5")
MAX_PER_RUN = int(os.environ.get("ENRICH_MAX_PER_RUN", "120"))
REENRICH_DAYS = 60
README_CHARS = 12000   # README 只取开头这么多字符：介绍和用法基本都在前面，后面多是 API 文档和更新日志
WORKERS = 4
SAVE_EVERY = 20

with open(os.path.join(ROOT, "config", "taxonomy.json"), encoding="utf-8") as f:
    TAX = json.load(f)
CAT_IDS = [c["id"] for c in TAX["categories"]]
PAIN_IDS = [p["id"] for p in TAX["pain_points"]]


def enum_list(values, description):
    return {"type": "array", "items": {"type": "string", "enum": values}, "description": description}


SCHEMA = {
    "type": "object",
    "properties": {
        "summary_zh": {"type": "string", "description": "一句话中文简介，40 字以内，说清楚它是什么、能干什么"},
        "features_zh": {"type": "array", "items": {"type": "string"}, "description": "3 到 5 条核心功能，每条 30 字以内"},
        "use_cases_zh": {"type": "array", "items": {"type": "string"}, "description": "1 到 3 个具体使用场景"},
        "hook_zh": {"type": "string", "description": "面向年轻人的一句话卖点，口语化，从痛点切入，25 字以内；对普通人没用的项目留空字符串"},
        "audience": enum_list(TAX["audience"], "目标用户"),
        "categories": {
            "type": "array",
            "description": "1 到 3 个类目，最相关的放第一个",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": CAT_IDS},
                    "sub": {"type": "string", "description": "该类目下的二级类目，必须从给定列表里选，没有合适的就留空字符串"},
                    "confidence": {"type": "number", "description": "0 到 1"},
                },
                "required": ["id", "sub", "confidence"],
                "additionalProperties": False,
            },
        },
        "pain_points": enum_list(PAIN_IDS, "最多 2 个它能解决的年轻人痛点，没有就给空数组"),
        "inputs": enum_list(TAX["io"], "它接收什么作为输入"),
        "outputs": enum_list(TAX["io"], "它产出什么"),
        "interfaces": enum_list(TAX["interfaces"], "可以通过哪些方式使用或调用它"),
        "batch_ok": {"type": "boolean", "description": "能否无人值守地批量运行（脚本化调用）"},
        "deploy": enum_list(TAX["deploy"], "安装或部署方式"),
        "requires": enum_list(TAX["requires"], "运行前提"),
        "difficulty": {"type": "string", "enum": ["1", "2", "3"], "description": "1 装完就能用；2 需要配置；3 需要开发能力"},
        "platforms": enum_list(TAX["platforms"], "支持的平台"),
        "cost": {"type": "string", "enum": TAX["cost"]},
        "newbie_friendly": {"type": "string", "enum": ["友好", "中等", "不友好"],
                            "description": "普通人能否直接用：有网页版或桌面应用、不用敲命令为友好；需要命令行为中等；需要部署或写代码为不友好"},
        "demo_url": {"type": "string", "description": "README 里给出的在线体验地址，没有就留空字符串"},
        "has_media": {"type": "boolean", "description": "README 里有没有截图、GIF 或演示视频"},
        "risk_note": {"type": "string", "description": "使用风险，比如依赖逆向接口可能失效、涉及平台账号有封号风险；没有就留空字符串"},
    },
    "required": ["summary_zh", "features_zh", "use_cases_zh", "hook_zh", "audience", "categories", "pain_points",
                 "inputs", "outputs", "interfaces", "batch_ok", "deploy", "requires", "difficulty", "platforms",
                 "cost", "newbie_friendly", "demo_url", "has_media", "risk_note"],
    "additionalProperties": False,
}


def build_system():
    cats = "\n".join("- %s（%s）：%s" % (c["id"], c["name"], "、".join(c["subs"]) or "—") for c in TAX["categories"])
    pains = "\n".join("- %s（%s）：%s" % (p["id"], p["name"], p["desc"]) for p in TAX["pain_points"])
    return """你在为一个中文 GitHub 开源项目看板整理项目信息。读者有两类：
1. 在 Threads 上分享 AI 工具心得的自媒体作者，关心这个项目能帮年轻人解决什么烦恼、普通人能不能上手；
2. 想把几个开源项目串联成自己工具的人，关心它的输入输出、调用方式和部署条件。

根据仓库信息和 README，按给定 JSON 结构填写。要求：
- 中文表达自然、具体，不要翻译腔，不要夸大；README 没提到的能力不要编。
- 类目按「用来干什么」判断，不按技术栈。Agent、MCP、CLI 是形态，不是用途：比如一个做 SEO 审计的 MCP Server 归 seo。只有给开发者写代码、部署、调试用的才归 dev。
- 二级类目必须从对应类目的列表里原样选择。
- 痛点只在项目确实能帮普通年轻人解决这个问题时才标，开发者工具和研究项目通常不标。
- inputs/outputs 从取值表里选最贴切的，没有输入就选「无」。
- 信息不足时保守填写：difficulty 取较高值，cost 不确定时选「需自备 API key」或「完全免费」中更符合 README 的一个。

类目（id（名称）：二级类目）：
%s

痛点（id（名称）：典型场景）：
%s""" % (cats, pains)


SYSTEM = build_system()


def fetch_readme(name):
    try:
        data = json.loads(request("https://api.github.com/repos/%s/readme" % name, headers=api_headers()))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise
    text = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
    return text[:README_CHARS]


def build_user(repo, readme):
    meta = {k: repo.get(k) for k in ("name", "description", "language", "topics", "stars", "homepage", "license")}
    return "仓库信息：\n%s\n\nREADME（可能截断）：\n%s" % (
        json.dumps(meta, ensure_ascii=False, indent=1), readme or "（没有 README）")


def clean(card):
    """模型偶尔会超出数量要求，这里统一截断、去重。"""
    seen, cats = set(), []
    subs = {c["id"]: c["subs"] for c in TAX["categories"]}
    for c in card["categories"]:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if c["sub"] not in subs[c["id"]]:
            c["sub"] = ""
        c["confidence"] = round(max(0.0, min(1.0, c["confidence"])), 2)
        cats.append(c)
    card["categories"] = cats[:3] or [{"id": "other", "sub": "", "confidence": 0.3}]
    card["pain_points"] = list(dict.fromkeys(card["pain_points"]))[:2]
    card["features_zh"] = card["features_zh"][:5]
    card["use_cases_zh"] = card["use_cases_zh"][:3]
    card["difficulty"] = int(card["difficulty"])
    for k in ("audience", "inputs", "outputs", "interfaces", "deploy", "requires", "platforms"):
        card[k] = list(dict.fromkeys(card[k]))
    return card


def enrich_one(client, repo):
    import anthropic
    readme = fetch_readme(repo["name"])
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            cache_control={"type": "ephemeral"},
            system=SYSTEM,
            messages=[{"role": "user", "content": build_user(repo, readme)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.BadRequestError as e:
        log("%s 请求被拒绝：%s" % (repo["name"], e.message))
        return None
    if resp.stop_reason == "refusal":
        log("%s 模型拒绝处理，跳过" % repo["name"])
        return {"skipped": "refusal"}
    if resp.stop_reason == "max_tokens":
        log("%s 输出被截断，下次再试" % repo["name"])
        return None
    text = next(b.text for b in resp.content if b.type == "text")
    return clean(json.loads(text))


def pick_candidates(repos, enriched, today):
    stale = (today - dt.timedelta(days=REENRICH_DAYS)).isoformat()
    todo = []
    for r in repos:
        e = enriched.get(r["name"])
        if e is None:
            todo.append(r)
        elif e.get("enriched_at", "") < stale and (r.get("pushed_at") or "") > e.get("enriched_at", ""):
            todo.append(r)
    todo.sort(key=lambda r: (r.get("d7") or 0, r["stars"]), reverse=True)
    return todo[:MAX_PER_RUN]


def main():
    repos = load("latest.json", {"repos": []})["repos"]
    if len(sys.argv) == 3 and sys.argv[1] == "--dry-run":
        repo = next((r for r in repos if r["name"] == sys.argv[2]), {"name": sys.argv[2]})
        print("== system ==\n%s\n\n== user ==\n%s" % (SYSTEM, build_user(repo, fetch_readme(repo["name"]))))
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("未设置 ANTHROPIC_API_KEY，跳过中文介绍生成")
        return

    import anthropic
    client = anthropic.Anthropic(max_retries=4)
    today = dt.datetime.now(dt.timezone.utc).date()
    enriched = load("enriched.json", {})
    todo = pick_candidates(repos, enriched, today)
    log("待处理 %d 个仓库（模型 %s）" % (len(todo), MODEL))

    done = 0
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(enrich_one, client, r): r for r in todo}
        for fut in cf.as_completed(futures):
            name = futures[fut]["name"]
            try:
                card = fut.result()
            except anthropic.AuthenticationError:
                log("ANTHROPIC_API_KEY 无效，停止")
                pool.shutdown(cancel_futures=True)
                break
            except (anthropic.APIError, urllib.error.URLError, ValueError) as e:
                log("%s 处理失败：%s" % (name, e))
                continue
            if card is None:
                continue
            card["enriched_at"] = today.isoformat()
            card["model"] = MODEL
            enriched[name] = card
            done += 1
            if done % SAVE_EVERY == 0:
                save("enriched.json", enriched)
                log("已完成 %d/%d" % (done, len(todo)))
    save("enriched.json", enriched)
    log("完成：本次新增/更新 %d 个，累计 %d 个" % (done, len(enriched)))


if __name__ == "__main__":
    main()
