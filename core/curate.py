"""L4 领域层：curator——偏好库的蒸馏与调档（V3，ADR-0013）。

01 票交付初始化蒸馏：旧偏好档案 + 主题词表 + 报名清单 signup 源全部标题 +
全部反馈流水 → 一次 LLM 蒸馏出偏好库初稿三件（当前理解 / 初批关键词 /
初批 llm 源关注）。02 票交付申请单裁决层与触发水位：纯函数、零 LLM、零
I/O——红线护栏全部固化于此，任何一道闸不过 = 整单作废、档案原样。03 票
交付调档链的取信侧：证据装配（纯函数）→ 调档提示词（常量）→ LLM 调用 →
回复容错解析；执行侧（快照 / 落盘 / 日志编排）在 patrol.run_curation，
快照工具在 store.snapshot_curation、回滚消费在 store.process_rollback
（05 票已把整链接进巡检主循环）。

分工：纯函数（解析 / 初稿组装 / 过目渲染 / 证据装配 / 申请单裁决 / 触发
判定 / 生效词表与打分渲染〔04 票〕）+ LLM 调用（容错口径同
recommend.llm_score：任何失败返回 (None, 原因)，调用方不写任何档案——安全
默认 = 不动，spec 决策 13）。
"""
import copy
import json
import os
import re
from datetime import datetime, timedelta

# ---------- 关键 20%：初始化蒸馏提示词（改这里 = 改偏好库的第一印象） ----------
# 口径（ADR-0013 初始化 + spec 实施决策 8/11/18）：
# - 种子不劳模型写（旧档案全文由代码照抄进 seed），它只产出三样：当前理解
#   （可整体重写）、初批关键词（owner:llm）、初批 llm 源关注（可空）
# - 关键词必须"字面会出现在活动标题/简介里"（决策 8）——抽象描述词零命中价值
# - llm 关注只许从报名清单【未开始】的里面挑（决策 18：ADR-0014 后已开始活动
#   对 llm 源零动向价值）；activity_id 原样抄清单，幻觉 id 由调用方过滤
# - 证据引用口径：反馈流水 fb#N、报名 signup:《标题》、档案自述 seed:档案
DISTILL_SYSTEM_PROMPT = """你是我的偏好管理助手（curator），现在做一次性的偏好初始化蒸馏。
我会给你我的偏好档案全文（我的自我描述）、主题词表（命中即必推）、我报名参加过的活动清单、以及我对推送过的活动的全部反馈流水。请蒸馏成"当前理解 + 初批关键词 + 初批关注"，规则：
- understanding：写给打分助手看的浓缩结论，5 句以内——我是什么人、稳定想参加什么类型的活动、明显排斥什么、报名行为与自我描述有没有出入
- keywords：5~15 个正信号关键词，每个 {"word": "…", "evidence": ["…"], "note": "可选一句话"}；word 必须是字面会出现在活动标题或简介里的词（如"电影""人工智能"，不要"有动手环节的活动"这类描述）；evidence 用 "fb#流水号"、"signup:《活动标题》" 或 "seed:档案" 标注依据
- watch：初批 llm 源关注，可空数组。只能从我报名清单里标了【未开始】的活动中挑、最多 5 个、挑"值得持续盯名额/截止/变更"的；每个 {"activity_id": "原样抄报名清单", "title": "…", "reason": "不超过 20 字"}；activity_id 不许编造
只输出一个 JSON 对象，不要任何其他文字或代码围栏，格式：
{"understanding": "…", "keywords": [{"word": "…", "evidence": ["…"], "note": "…"}], "watch": [{"activity_id": "…", "title": "…", "reason": "…"}]}"""

BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"


def distill(evidence: dict, llm: dict) -> tuple[dict | None, str]:
    """一次性蒸馏（V3 冷启动）。evidence = {"prefs_text", "themes", "signups",
    "feedback"}；llm = {"model", "base_url"}（同打分配置）。失败返回 (None, 原因)，
    成功返回 (初稿三件 {"understanding","keywords","watch"}, "")。不抛异常。"""
    key = os.environ.get("ERKE_LLM_API_KEY")
    if not key:
        return None, "未设置 ERKE_LLM_API_KEY——无法蒸馏"
    try:  # import 放函数内：没装 openai 也不影响其余功能（同 recommend）
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=(llm or {}).get("base_url", BASE_URL))
        reply = client.chat.completions.create(
            model=(llm or {}).get("model", "glm-4-flash"),
            temperature=0.2,  # 蒸馏要稳定不要发散（与打分同口径）
            messages=[{"role": "system", "content": DISTILL_SYSTEM_PROMPT},
                      {"role": "user", "content": _user_content(evidence)}],
        )
        obj, why = parse_distilled(reply.choices[0].message.content or "")
        if why:
            return None, why
        return obj, ""
    except Exception as e:  # 网络/解析失败一律降级（安全默认 = 不动）
        return None, f"蒸馏失败：{e!r}"


def parse_distilled(text: str) -> tuple[dict | None, str]:
    """容错解析蒸馏回复（与 recommend._parse_results 同款先例）：
    围栏/说明文字掐头去尾 → 整包 JSON → 尾逗号修复重试 → 坏 JSON 逐片段抢救
    （关键词/关注条目 + 理解文本，能抢回多少算多少）——彻底解析不出才失败。"""
    text = _strip_fence(text)
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return _normalize(obj), ""
    m = re.search(r'"understanding"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    keywords = _salvage(text, "word")
    watch = _salvage(text, "activity_id")
    if m or keywords or watch:
        return {"understanding": _unquote(m.group(1)) if m else "",
                "keywords": keywords, "watch": watch}, ""
    return None, "无法从模型返回中解析出蒸馏结果"


def _kw_entry(k: dict, now_iso: str) -> dict:
    """蒸馏/申请单的关键词条目 → 活数组条目（owner 恒为 llm，两处同一口径）。"""
    entry = {"word": k["word"], "owner": "llm", "added_at": now_iso,
             "evidence": [str(x) for x in k.get("evidence", [])]}
    if k.get("note"):
        entry["note"] = str(k["note"])
    return entry


def build_initial_profile(distilled: dict, seed_text: str, seed_keywords: list[str],
                          seed_excludes: list[str], now_iso: str,
                          watch_candidates: dict[str, str]) -> tuple[dict, list[dict]]:
    """初稿三件 → (偏好库全文, 初批关注 picks)（schema 见 spec 实施决策 2）。
    种子照抄旧档；关键词落 owner:llm 活数组；picks 已按 watch_candidates
    （id→标题，调用方按"未开始"预筛，决策 18）过滤——幻觉 id 与不可点名目标
    在此丢弃，调用方直接写关注清单。watch 本身不进偏好库（llm 源关注写
    关注清单，决策 8）。"""
    keywords = [_kw_entry(k, now_iso) for k in distilled.get("keywords", [])]
    picks = []
    for w in distilled.get("watch", []):
        aid = str(w.get("activity_id", ""))
        if aid in watch_candidates:
            picks.append({"activity_id": aid,
                          "title": str(w.get("title") or watch_candidates[aid]),
                          "reason": str(w.get("reason", ""))})
    profile = {"schema": 1,
               "seed": {"copied_at": now_iso, "text": seed_text,
                        "keywords": list(seed_keywords), "excludes": list(seed_excludes)},
               "understanding": str(distilled.get("understanding", "")),
               "keywords": keywords, "excludes": [],
               "corrections": [], "curations": []}
    return profile, picks


def render_review(profile: dict, picks: list[dict]) -> str:
    """初稿过目文本（初始化命令终端打印的只读展示，不进推送）。"""
    seed = profile.get("seed", {})
    keywords = profile.get("keywords", [])
    lines = [f"· 种子：旧档案全文照抄（{len(seed.get('text', ''))} 字）· "
             f"种子关键词 {len(seed.get('keywords', []))} 个 · "
             f"种子排除词 {len(seed.get('excludes', []))} 个（种子对模型永远只读）",
             f"· 当前理解：{profile.get('understanding', '') or '（空）'}",
             "· 初批关键词（owner:llm；生效词表 = 种子 ∪ 活数组）："
             + ("、".join(f"{k['word']}（{'，'.join(k.get('evidence', [])) or '无标注'}）"
                          for k in keywords) or "（空）"),
             f"· 初批 llm 关注 {len(picks)} 个（写入关注清单，动向效力同兴趣源）："]
    lines += [f"    - 《{p.get('title') or p.get('activity_id')}》{p.get('reason', '')}"
              for p in picks] or ["    （空）"]
    lines.append("· 修正记录 / 调档记录：（空——首条调档由巡检在出现新证据后触发）")
    return "\n".join(lines)


def _user_content(evidence: dict) -> str:
    themes = "、".join(evidence.get("themes") or [])
    feedback = evidence.get("feedback") or []
    return ("\n".join([
        f"# 我的偏好档案（全文）\n\n{evidence.get('prefs_text', '')}\n",
        f"# 我的主题词表（命中即必推）\n\n{themes or '（空）'}\n",
        "# 我报名参加过的活动（【未开始】的才可作为初批关注候选）\n\n"
        + json.dumps(evidence.get("signups") or [], ensure_ascii=False) + "\n",
        "# 反馈流水（全部；fb#编号 即证据引用口径）\n\n"
        + ("\n".join(f"fb#{f.get('seq', '?')} {f.get('time', '')} 《{f.get('title', '')}》"
                     f"{'感兴趣' if f.get('action') == 'interest' else '不感兴趣'}"
                     for f in feedback) or "（空）"),
    ]))


def _normalize(obj: dict) -> dict:
    """整包解析成功后的形状收口：缺 word/activity_id 的条目、非对象条目一律丢弃。"""
    keywords = [k for k in obj.get("keywords", []) or []
                if isinstance(k, dict) and isinstance(k.get("word"), str) and k["word"]]
    watch = [w for w in obj.get("watch", []) or []
             if isinstance(w, dict) and isinstance(w.get("activity_id"), str)
             and w["activity_id"]]
    return {"understanding": str(obj.get("understanding", "")),
            "keywords": keywords, "watch": watch}


def _salvage(text: str, key: str) -> list[dict]:
    """坏 JSON 抢救：逐 {...} 浅层片段回收含 key 的合法条目（同打分抢救先例）。"""
    out = []
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            one = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(one, dict) and isinstance(one.get(key), str) and one[key]:
            out.append(one)
    return out


def _unquote(body: str) -> str:
    """把 JSON 字符串字面量的内芯还原成文本（转义处理）；还原不了原样返回。"""
    try:
        return json.loads(f'"{body}"')
    except ValueError:
        return body


def _strip_fence(text: str) -> str:
    """模型偶尔在 JSON 外裹 ```json 围栏或说明文字，掐头去尾只留花括号之间
    （不赌端点的 response_format——学习记录 0005 的教训，同 recommend）。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


# ---------- 申请单裁决与触发水位（02 票；spec 决策 3/4/18——红线全部固化于此） ----------
# 纯函数、零 LLM、零 I/O：输入偏好库快照 + 申请单 + 真实世界事实（当前活动
# 全集、未开始集、关注清单）+ 配置上限，输出变更集或整单作废 + 原因。任何
# 一道闸不过 = 整单作废、档案原样（安全默认，spec 决策 13）——不降级、不部分应用。

# 申请单白名单五键（spec 决策 3）。刻意没有 excludes / blacklist / themes 通道——
# 结构即护栏：排除词、黑名单、主题词表永远只属于用户（ADR-0013 立法），
# 带任何计划外字段的申请单在 schema 闸即废。
REQUEST_KEYS = ("keywords", "watch", "understanding", "corrections", "rationale")


def last_watermark(profile: dict) -> tuple[int, int]:
    """curations 尾条的证据水位（无调档史 → 基线 0/0）。03 票起公开：调档链
    用它切"上次调档以来"的报名增删证据段（feed 按水位计数切片）。"""
    cur = profile.get("curations") or []
    wm = (cur[-1].get("watermark") or {}) if cur else {}
    return wm.get("feedback_seq") or 0, wm.get("signup_changes") or 0


def should_curate(profile: dict, feedback_seq: int, signup_changes: int,
                  today: str) -> tuple[bool, str]:
    """触发水位判定（spec 决策 4）：上次调档以来 feedback seq 或报名增删计数
    任一前进才触发，且每日最多一次。返回 (触发?, 原因行)——原因行进决策日志
    （user story 27：出问题先看日志就知道它为什么动 / 为什么没动）。

    水位口径：curations 尾条 watermark 是两条单调计数器——feedback_seq 取
    反馈流水最大 seq；signup_changes 是调用方持久累计的报名增删总条数（存入
    与比较的都是累计值，前进 = 上次调档以来又有增删）。无调档史时基线 =
    0/0（初始化蒸馏不算调档——票面原义"上次调档以来"，无上次即全部历史
    都算新证据，首次调档由最早一次巡检完成）。
    """
    cur = profile.get("curations") or []
    if cur and str(cur[-1].get("at", ""))[:10] == today:
        return False, f"今日已调档（{cur[-1].get('at')}）——每日最多一次，明日再看水位"
    base_seq, base_chg = last_watermark(profile)
    if feedback_seq > base_seq or signup_changes > base_chg:
        return True, (f"水位前进：feedback_seq {base_seq}→{feedback_seq}，"
                      f"报名增删累计 {base_chg}→{signup_changes}")
    return False, (f"水位未动（feedback_seq={feedback_seq}，报名增删累计={signup_changes}）"
                   "——无新证据不空转")


def update_signup_feed(feed: dict, registration_ids: list, titles: dict,
                       now_iso: str) -> tuple[dict, list[str]]:
    """报名增删流水维护（05 票，spec 决策 4 水位的 signup 侧计数器；纯函数）。

    feed = {"last": {活动id: 标题}, "entries": [{"title","action":"add|remove",
    "at"},…]}——entries 只追加、len 即累计水位计数器（03 票契约：调档链按
    curations 尾条水位切片），因此不做修剪：一年几十条的量级，修剪反而会
    撬动计数器。本轮报名名单与 last 比对：新报名记 add、消失记 remove；
    首轮（last 为空）把全部现存报名计为 add——无调档史即全部历史都算新
    证据，首次调档由初始化后的最早一次巡检完成（02 票基线 0/0 的口径）。
    标题优先取当轮活动表，活动已下架时回退 last 里的旧标题（增删行可读）。
    返回 (新 feed, 日志行 0~1 条)；不落盘，持久化归巡检（演练不写）。
    """
    new = {"last": dict(feed.get("last") or {}), "entries": list(feed.get("entries") or [])}
    prev = new["last"]
    cur: dict[str, str] = {}
    for rid in dict.fromkeys(str(r) for r in registration_ids or []):
        cur[rid] = str(titles.get(rid) or prev.get(rid) or "")
    added = [aid for aid in cur if aid not in prev]
    removed = [aid for aid in prev if aid not in cur]
    for aid in added:
        new["entries"].append({"title": cur[aid], "action": "add", "at": now_iso})
    for aid in removed:
        new["entries"].append({"title": prev[aid], "action": "remove", "at": now_iso})
    new["last"] = cur
    if not prev and cur:
        return new, [f"报名增删流水建立基线：现存报名 {len(cur)} 条全部计为新证据（偏好库初始化后首巡）"]
    if added or removed:
        return new, [f"报名增删：+{len(added)} −{len(removed)}（累计 {len(new['entries'])} 条）"]
    return new, []


def adjudicate(profile: dict, request: dict, real_ids: set, not_started_ids: set,
               watch_state: dict, limits: dict, now_iso: str,
               watermark: dict) -> tuple[dict | None, str]:
    """裁决一张调档申请单，五道闸按序（spec 决策 3）：
    schema → owner（keywords.remove 只许 owner:llm 的词；watch.remove 只许
    llm 源条目）→ 幅度（词 ±keyword_step、关注 ±watch_step，按分向计：add、
    remove 各自 ≤ 上限）→ 幻觉 id（watch.add 必须在 real_ids 当前活动全集内
    真实存在）→ 时限（watch.add 必须在 not_started_ids 未开始集内——已开始/
    未知状态对 llm 源零动向价值，ADR-0014 后维持沉寂；调用方按 01 票同款
    从严口径预筛：快照缺失/无开始时刻不算未开始）。

    任一不过 = (None, 原因)，偏好库原样；全过 = (变更集, "")。变更集 =
    {"profile": 新偏好库（curations 已追加本次记录，自带证据水位），
    "watch_add": [{activity_id,title,reason}], "watch_remove": [id]}——
    落盘与 watch 应用归调用方（03/05 票），本函数零 I/O、不改任何入参。

    limits = {"keyword_step": 3, "watch_step": 2}（config [curation] 节由
    03 票接线，缺键按 spec 决策 12 缺省值）。watermark = 本次触发时的当前
    水位 {"feedback_seq": N, "signup_changes": N}，原样记入 curations。
    """
    try:
        seq, chg = int(watermark["feedback_seq"]), int(watermark["signup_changes"])
    except (KeyError, TypeError, ValueError):
        return None, "水位参数不合法（须含整数 feedback_seq / signup_changes）"
    try:
        kw_step = int(limits.get("keyword_step", 3))
        w_step = int(limits.get("watch_step", 2))
    except (TypeError, ValueError):
        return None, "配置上限不合法（keyword_step / watch_step 须为整数）"
    errs = _request_errors(request)
    if errs:
        return None, "申请单 schema 不合法（整单作废）：" + "；".join(errs)
    seed_words = set(profile.get("seed", {}).get("keywords") or [])
    for w in request["keywords"]["remove"]:  # 闸二·owner：种子与 user 条目对 LLM 只读
        if w in seed_words:
            return None, f"越权：关键词『{w}』是种子词——种子对 curator 永远只读"
        entries = [e for e in profile.get("keywords", []) if e.get("word") == w]
        if not entries:
            return None, f"越权：remove 目标关键词『{w}』不在活数组（幻觉或已删）"
        if not any(e.get("owner") == "llm" for e in entries):
            return None, f"越权：关键词『{w}』非 owner:llm——user 手写条目只读"
    for aid in request["watch"]["remove"]:
        if "llm" not in ((watch_state.get(aid) or {}).get("sources") or []):
            return None, f"越权：watch.remove 目标 {aid} 不是 llm 源关注条目"
    for name, n, cap in (("keywords.add", len(request["keywords"]["add"]), kw_step),
                         ("keywords.remove", len(request["keywords"]["remove"]), kw_step),
                         ("watch.add", len(request["watch"]["add"]), w_step),
                         ("watch.remove", len(request["watch"]["remove"]), w_step)):
        if n > cap:  # 闸三·幅度：偏好不被单次大幅漂移（user story 11）
            return None, f"幅度超限：{name} {n} 条 > 上限 {cap}——整单作废"
    for w in request["watch"]["add"]:  # 闸四·幻觉 id：关注清单不进不存在的活动
        if w["activity_id"] not in real_ids:
            return None, (f"幻觉活动 id：{w['activity_id']}（{w.get('title', '')}）"
                          "不在当前活动全集")
    for w in request["watch"]["add"]:  # 闸五·时限：只许点名未开始活动（决策 18）
        if w["activity_id"] not in not_started_ids:
            return None, (f"时限闸：{w['activity_id']}（{w.get('title', '')}）"
                          "不是未开始活动——已开始/未知状态零动向价值，不可点名")
    return _apply(profile, request, now_iso, seq, chg), ""


def _apply(profile: dict, request: dict, now_iso: str, seq: int, chg: int) -> dict:
    """五道闸全过后组装变更集（纯构造，不校验——闸已把关）。确定性口径：
    关键词先删后加（同词先删后加 = 换证据重写）；已在活数组的词幂等跳过。"""
    new = copy.deepcopy(profile)
    removed = set(request["keywords"]["remove"])
    new["keywords"] = [e for e in new["keywords"]
                       if not (e.get("word") in removed and e.get("owner") == "llm")]
    present = {e.get("word") for e in new["keywords"]}
    added = []
    for k in request["keywords"]["add"]:
        if k["word"] in present:
            continue
        present.add(k["word"])
        added.append(_kw_entry(k, now_iso))
    new["keywords"] += added
    new["understanding"] = request["understanding"]  # 当前理解整体替换（user story 19）
    for c in request["corrections"]:  # 偏好漂移以修正记录软表达（user story 16）
        new.setdefault("corrections", []).append(
            {"at": now_iso, "about": c["about"], "finding": c["finding"],
             "evidence": [str(x) for x in c.get("evidence", [])]})
    prev_seq, prev_chg = last_watermark(profile)
    new.setdefault("curations", []).append({
        "at": now_iso,
        "trigger": f"feedback_seq {prev_seq}→{seq}，报名增删累计 {prev_chg}→{chg}",
        "applied": {"kw_add": [e["word"] for e in added],
                    "kw_remove": list(dict.fromkeys(request["keywords"]["remove"])),
                    "watch_add": [w["activity_id"] for w in request["watch"]["add"]],
                    "watch_remove": list(dict.fromkeys(request["watch"]["remove"])),
                    "understanding": True,
                    "corrections": len(request["corrections"])},
        "rationale": request["rationale"],
        "watermark": {"feedback_seq": seq, "signup_changes": chg}})
    return {"profile": new,
            "watch_add": [{"activity_id": w["activity_id"],
                           "title": str(w.get("title", "")),
                           "reason": str(w.get("reason", ""))}
                          for w in request["watch"]["add"]],
            "watch_remove": list(dict.fromkeys(request["watch"]["remove"]))}


def _request_errors(r) -> list[str]:
    """申请单 schema 闸（spec 决策 3 的结构面）：五键白名单 + 各字段形状。
    宽松度对齐 store.profile_errors：类型错 / 缺键报错；计划外键零容忍——
    排除词通道必须在结构上就不存在，而不是校验时被拒绝。"""
    if not isinstance(r, dict):
        return ["申请单必须是 JSON 对象"]
    errs: list[str] = []
    extra = sorted(set(r) - set(REQUEST_KEYS))
    if extra:
        errs.append(f"计划外字段 {extra}——申请单没有这些通道（结构即护栏）")
    missing = [k for k in REQUEST_KEYS if k not in r]
    if missing:
        errs.append(f"缺字段 {missing}")
    if errs:
        return errs
    kw, w = r["keywords"], r["watch"]
    if not isinstance(kw, dict):
        errs.append("keywords 必须为对象 {add, remove}")
    elif not (isinstance(kw.get("add"), list) and isinstance(kw.get("remove"), list)):
        errs.append("keywords.add / keywords.remove 必须为数组")
    else:
        for i, k in enumerate(kw["add"]):
            if not (isinstance(k, dict) and isinstance(k.get("word"), str) and k["word"]):
                errs.append(f"keywords.add[{i}] 缺合法 word")
            elif not _str_seq(k.get("evidence", [])):
                errs.append(f"keywords.add[{i}].evidence 必须为字符串数组")
            elif not isinstance(k.get("note", ""), str):
                errs.append(f"keywords.add[{i}].note 必须为字符串")
        errs += [f"keywords.remove[{i}] 必须为非空字符串"
                 for i, word in enumerate(kw["remove"])
                 if not (isinstance(word, str) and word)]
    if not isinstance(w, dict):
        errs.append("watch 必须为对象 {add, remove}")
    elif not (isinstance(w.get("add"), list) and isinstance(w.get("remove"), list)):
        errs.append("watch.add / watch.remove 必须为数组")
    else:
        for i, a in enumerate(w["add"]):
            if not (isinstance(a, dict) and isinstance(a.get("activity_id"), str)
                    and a["activity_id"]):
                errs.append(f"watch.add[{i}] 缺合法 activity_id")
            elif not (isinstance(a.get("title", ""), str)
                      and isinstance(a.get("reason", ""), str)):
                errs.append(f"watch.add[{i}].title / reason 必须为字符串")
        errs += [f"watch.remove[{i}] 必须为非空字符串"
                 for i, aid in enumerate(w["remove"])
                 if not (isinstance(aid, str) and aid)]
    if not isinstance(r["understanding"], str):
        errs.append("understanding 必须为字符串（当前理解整体替换）")
    if not isinstance(r["rationale"], str):
        errs.append("rationale 必须为字符串")
    if not isinstance(r["corrections"], list):
        errs.append("corrections 必须为数组")
    else:
        for i, c in enumerate(r["corrections"]):
            if not isinstance(c, dict):
                errs.append(f"corrections[{i}] 必须为对象")
            elif not (isinstance(c.get("about"), str) and isinstance(c.get("finding"), str)):
                errs.append(f"corrections[{i}] 需字符串 about / finding")
            elif not _str_seq(c.get("evidence", [])):
                errs.append(f"corrections[{i}].evidence 必须为字符串数组")
    return errs


def _str_seq(v) -> bool:
    """字符串数组判定（与 store._str_list 同形：core 禁 import store，各留一份）。"""
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


# ---------- 调档链取信侧（03 票；spec 决策 5/8/13/17/18） ----------
# 编排在 patrol.run_curation；快照在 store.snapshot_curation。本节全部零落盘：
# 任何失败返回 (None, 原因)，调用方不写任何档案（安全默认 = 不动）。

# ---------- 关键 20%：调档提示词（改这里 = 改偏好库的进化方向） ----------
# 口径（ADR-0013 调档 + spec 实施决策 3/8/18）：
# - 种子（我的自我描述）永远只读；owner:user 的词只读——remove 只许动
#   owner:llm 的活数组词，否则裁决层整单作废（提示词只是劝说，闸在代码）
# - 关键词必须"字面会出现在活动标题/简介里"（决策 8）——抽象描述词零命中价值
# - 关注点名只许从未开始活动里选（决策 18）：ADR-0014 后已开始活动对 llm 源
#   零动向价值；想追已开始母活动的内容更新，正道是把词写进关键词
# - 申请单五键白名单：没有 excludes/blacklist/themes 通道——"永远别推"
#   只属于用户，带计划外字段在裁决层 schema 闸即废（结构即护栏）
# - 没有值得调整的就交空单（空数组 + 原 understanding）——空单照走裁决，
#   记一条零变更调档把水位吃掉，避免同批证据反复上岗
# - 证据引用口径与初始化蒸馏同款：反馈 fb#N、报名 signup:《标题》
CURATION_SYSTEM_PROMPT = """你是我的偏好管理助手（curator），现在做一次例行的偏好调档。
我会给你：我的偏好库现状（种子自我描述【只读】、当前理解、活数组关键词、修正记录）、近期反馈流水、上次调档以来我的报名增删、近期推送决策摘要、我当前盯的活动、以及可点名的未开始活动清单。请基于新证据产出一张调档申请单，规则：
- keywords.add：正信号关键词，每个 {"word": "…", "evidence": ["…"], "note": "可选一句话"}；word 必须是字面会出现在活动标题或简介里的词（如"电影""辩论"，不要"有动手环节的活动"这类描述）；evidence 用 "fb#流水号"、"signup:《活动标题》" 标注依据
- keywords.remove：只许移除活数组里 owner:llm 的词；种子词与 owner:user 的词永远只读
- watch.add：新盯的活动，只能从【可点名的未开始活动】清单里选（activity_id 原样抄清单、不许编造）——已开始的活动对它零动向价值，点名会被时限闸作废；想追某长期活动的内容更新，正道是加关键词
- watch.remove：只许移除【我当前盯的活动】清单里的
- understanding：重写后的"当前理解"全文（5 句以内，写给打分助手看的浓缩结论）
- corrections：偏好漂移的软修正，每个 {"about": "…", "finding": "…", "evidence": ["…"]}
- rationale：一段话说清本次为什么调（引用证据编号）
没有值得调整的就输出空数组与原 understanding。只输出一个 JSON 对象，不要任何其他文字或代码围栏，格式：
{"keywords": {"add": [], "remove": []}, "watch": {"add": [{"activity_id": "…", "title": "…", "reason": "不超过 20 字"}], "remove": []}, "understanding": "…", "corrections": [{"about": "…", "finding": "…", "evidence": []}], "rationale": "…"}"""


def assemble_evidence(profile: dict, feedback: list, signup_changes: list,
                      snapshots: dict, llm_watch: list, not_started: list,
                      windows: dict, now) -> str:
    """调档证据装配（纯函数，spec 决策 5）：偏好库现状 + 三路证据 + 关注两侧
    清单，各自成节进 prompt 用户内容。个人标识（流水 from 列）不进 prompt
    ——只渲染 seq/time/title/action 四列。

    窗口天数读 windows = {"feedback_window_days": 30, "decision_window_days": 7}
    （config [curation] 节，类型/越界坏值一律回退缺省——0/10000 这类手误
    不会变成空证据或万年窗）。signup_changes = 上次调档以来的
    报名增删（调用方按 curations 尾条水位切片），[{"title", "action":
    "add|remove", "at"}]；llm_watch = 当前 llm 源关注 [{"activity_id",
    "title"}]；not_started = 可点名的未开始活动 [{"activity_id", "title",
    "start_time"}]（从严口径：无开始时刻不算未开始）。"""
    fb_days = _window_days((windows or {}).get("feedback_window_days"), 30)
    dec_days = _window_days((windows or {}).get("decision_window_days"), 7)
    seed = profile.get("seed", {})
    kw_line = ("、".join(f"{k.get('word')}（owner:{k.get('owner')}）"
                         for k in profile.get("keywords") or []) or "（空）")
    corr_lines = _correction_lines(profile)
    cutoff = now - timedelta(days=fb_days)
    fb_lines = _feedback_lines(feedback, cutoff)
    sign_lines = ([f"{'＋' if c.get('action') == 'add' else '－'} 《{c.get('title', '')}》"
                   f"（{str(c.get('at', ''))[:10]}）"
                   for c in signup_changes or []] or ["（无）"])
    summary = summarize_decisions(snapshots, dec_days, now)
    watch_lines = ([f"{w.get('activity_id')} 《{w.get('title', '')}》"
                    for w in llm_watch or []] or ["（空）"])
    cand_lines = ([f"{a.get('activity_id')} 《{a.get('title', '')}》"
                   f"（{str(a.get('start_time', ''))[:16]} 开始）"
                   for a in not_started or []] or ["（空）"])
    return "\n".join([
        "# 偏好库现状（种子 = 我的自我描述，对你永远只读）\n\n"
        f"{seed.get('text', '')}\n\n当前理解：{profile.get('understanding', '') or '（空）'}\n"
        f"活数组关键词（remove 只许动 owner:llm 的）：{kw_line}\n"
        "修正记录（近 5 条）：\n" + "\n".join(corr_lines) + "\n",
        f"# 反馈流水（近 {fb_days} 天滚动窗；fb#编号 即证据引用口径）\n\n"
        + "\n".join(fb_lines) + "\n",
        "# 报名增删（上次调档以来）\n\n" + "\n".join(sign_lines) + "\n",
        f"# 推送决策摘要（近 {dec_days} 天）\n\n" + ("\n".join(summary) or "（空）") + "\n",
        "# 我当前盯的活动（llm 源关注；watch.remove 只许从这里选）\n\n"
        + "\n".join(watch_lines) + "\n",
        "# 可点名的未开始活动（watch.add 只许从这里选，activity_id 原样抄）\n\n"
        + "\n".join(cand_lines),
    ])


_KIND_CONCLUSIONS = {"theme": "主题命中 → 必推已送",
                     "bootstrap": "建库未决策",
                     "expired": "已过期（已开始或截止已过，未打分）"}


def _window_days(v, default: int) -> int:
    """窗口天数收口：类型坏或越界（agent._load_config 校验范围 1~365 之外）
    一律回退缺省——配置手误不静默掏空证据。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if 1 <= n <= 365 else default


def _feedback_lines(feedback: list, cutoff) -> list[str]:
    """反馈流水渲染行（调档证据与打分渲染共用，04 票收敛）：seq/time/title/
    action 四列——from 个人标识不进 prompt；时间缺/坏：宁可多给证据，不静默丢。"""
    return ([f"fb#{f.get('seq', '?')} {str(f.get('time', ''))[:16]} "
             f"《{f.get('title', '')}》"
             f"{'感兴趣' if f.get('action') == 'interest' else '不感兴趣'}"
             for f in feedback or []
             if _dt(f.get("time")) is None or _dt(f.get("time")) >= cutoff]
            or ["（空）"])


def _correction_lines(profile: dict) -> list[str]:
    """修正记录渲染行（调档证据与打分渲染共用，04 票收敛）：近 5 条，
    「- [日期] about：finding」一行一条；空则占位。"""
    return ([f"- [{str(c.get('at', ''))[:10]}] {c.get('about', '')}：{c.get('finding', '')}"
             for c in (profile.get("corrections") or [])[-5:]] or ["（空）"])


def summarize_decisions(snapshots: dict, days: int, now) -> list[str]:
    """决策账本近 N 天摘要行（spec 决策 5/17）：rec/filtered 带三列分数
    （llm/keyword/score）与是否推过；theme/bootstrap/expired 无三列分数，
    只列标题与结论——不缺席、不报错。按决策时刻排序，标题取快照活动。"""
    cutoff = now - timedelta(days=days)
    rows = []
    for s in (snapshots or {}).values():
        d = s.get("decision")
        if not isinstance(d, dict) or not d.get("kind"):
            continue
        at = _dt(d.get("at"))
        if at is None or at < cutoff:
            continue
        title = ((s.get("activity") or {}).get("title")) or "?"
        if d["kind"] in _KIND_CONCLUSIONS:
            rows.append((at, f"《{title}》{_KIND_CONCLUSIONS[d['kind']]}"))
        else:  # rec / filtered（及未来带分数的新 kind）：三列分数 + 是否推过
            verdict = "已推送" if d["kind"] == "rec" else "未推（低于线）"
            rows.append((at, f"《{title}》综合 {d.get('score')}/10"
                         f"（LLM {d.get('llm')}＋关键词 {d.get('keyword')}）→ {verdict}"))
    return [line for _at, line in sorted(rows)]


def curate_request(profile: dict, feedback: list, signup_changes: list,
                   snapshots: dict, llm_watch: list, not_started: list,
                   windows: dict, now, llm: dict | None = None,
                   call=None) -> tuple[dict | None, str]:
    """发起一次调档：装配证据 → 发调档提示词 → 容错解析申请单。
    返回 (申请单, "") 或 (None, 原因)——任何失败不抛异常、调用方不落盘
    （spec 决策 13）。call = 可注入的对话函数 (system, user) → (回复文本,
    失败原因)，缺省走 OpenAI 兼容网络——单测注入罐头回复，网络本体不进
    单测（与打分同法）。申请单原样交裁决层：解析只管文本→JSON，五道闸
    在 adjudicate。"""
    user = assemble_evidence(profile, feedback, signup_changes, snapshots,
                             llm_watch, not_started, windows, now)
    if call is None:
        key = os.environ.get("ERKE_LLM_API_KEY")
        if not key:
            return None, "未设置 ERKE_LLM_API_KEY——无法调档"
        call = lambda system, content: _chat(system, content, key, llm or {})  # noqa: E731
    text, why = call(CURATION_SYSTEM_PROMPT, user)
    if why:
        return None, why
    return parse_curated(text or "", str(profile.get("understanding", "")))


def _chat(system: str, content: str, key: str, llm: dict) -> tuple[str, str]:
    """OpenAI 兼容调用（网络本体，口径同 distill / recommend.llm_score）。"""
    try:  # import 放函数内：没装 openai 也不影响其余功能（同 recommend）
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=llm.get("base_url", BASE_URL))
        reply = client.chat.completions.create(
            model=llm.get("model", "glm-4-flash"),
            temperature=0.2,  # 调档要稳定不要发散（与打分同口径）
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": content}])
        return reply.choices[0].message.content or "", ""
    except Exception as e:  # 网络失败一律降级（安全默认 = 不动，spec 决策 13）
        return "", f"调档 LLM 调用失败：{e!r}"


def parse_curated(text: str, current_understanding: str = "") -> tuple[dict | None, str]:
    """容错解析调档申请单（与 recommend._parse_results 同款先例）：
    围栏/说明文字掐头去尾 → 整包 JSON → 尾逗号修复重试 → 坏 JSON 抢救
    （回收关键词/关注/修正/理解；remove 清单回收不了——丢弃属安全方向：
    漏删的词下轮还在活数组里，模型看得见可再删）。understanding 抢救不出
    时回退 current_understanding——不拿空串抹掉现有理解。解析宽容不等于
    执行宽容：产出仍要过裁决五道闸。彻底解析不出才 (None, 原因)——档案
    原样、下轮重试（spec 决策 13）。"""
    text = _strip_fence(text)
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj, ""  # 原样交裁决：形状不对由 schema 闸整单作废
    m = re.search(r'"understanding"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    adds, watches = _salvage(text, "word"), _salvage(text, "activity_id")
    corrections = _salvage(text, "about")
    if not (m or adds or watches or corrections):
        return None, "无法从模型返回中解析出调档申请单"
    rat = re.search(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return {"keywords": {"add": adds, "remove": []},
            "watch": {"add": watches, "remove": []},
            "understanding": _unquote(m.group(1)) if m else current_understanding,
            "corrections": corrections,
            "rationale": _unquote(rat.group(1)) if rat else ""}, ""


def _dt(v):
    """ISO 时刻解析（store.parse_dt 的 core 本地版——core 禁 import store，各留一份）。"""
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


# ---------- 打分链换料与生效词表（04 票；spec 决策 6/7/16） ----------
# 渲染纯函数：偏好库 → 打分 prompt 的偏好段 + 流水段成品文本。打分模块
# （recommend.llm_score / scoring.enrich）只收成品、不感知偏好库结构
# （spec 决策 1）；词表提取也从偏好库 JSON 取——不经文本行解析，"当前理解"
# 这类自由文本里恰好以「关键词」开头的行污染不了生效词表。

def _dedup_union(seed_words: list, live: list) -> list[str]:
    """并集保序去重：种子在前、活数组在后——活数组的增删不动种子半个字。"""
    out = [w for w in seed_words or [] if isinstance(w, str) and w]
    seen = set(out)
    for e in live or []:
        w = e.get("word") if isinstance(e, dict) else None
        if isinstance(w, str) and w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def effective_keywords(profile: dict) -> list[str]:
    """生效关键词 = seed.keywords ∪ 活数组词（spec 决策 7，user story 18：
    旧词不因 LLM 新增而失效）。"""
    seed = profile.get("seed") or {}
    return _dedup_union(seed.get("keywords"), profile.get("keywords"))


def effective_excludes(profile: dict) -> list[str]:
    """生效排除词 = seed.excludes ∪ 活数组词。活数组只可能 owner:user
    （schema 闸固化——LLM 无份，spec 决策 7），硬排除通道永远只属于用户。"""
    seed = profile.get("seed") or {}
    return _dedup_union(seed.get("excludes"), profile.get("excludes"))


def render_scoring_prefs(profile: dict, feedback: list, windows: dict, now) -> str:
    """偏好库 → 打分 prompt 的偏好段 + 流水段成品文本（spec 决策 6）。
    偏好段四节：种子文本（自我描述原文照抄）→ 当前理解 → 修正记录（近 5 条）
    → 生效词表行（关键词/排除词并集渲染）；流水段独立成节（近 N 天滚动窗，
    窗口读 windows = {"feedback_window_days": 30}，坏值回退缺省）。"""
    fb_days = _window_days((windows or {}).get("feedback_window_days"), 30)
    seed = profile.get("seed") or {}
    corr_lines = _correction_lines(profile)
    kw_line = "、".join(effective_keywords(profile)) or "（空）"
    ex_line = "、".join(effective_excludes(profile)) or "（空）"
    fb_lines = _feedback_lines(feedback, now - timedelta(days=fb_days))
    return "\n".join([
        seed.get("text", ""),
        "# 当前理解（curator 蒸馏结论，随调档整体重写）\n\n"
        + (profile.get("understanding") or "（空）") + "\n",
        "# 修正记录（近 5 条，偏好漂移的软表达）\n\n" + "\n".join(corr_lines) + "\n",
        "# 生效词表（关键词 = 种子 ∪ 活数组；排除词 = 种子 ∪ 手写）\n\n"
        f"关键词：{kw_line}\n排除词：{ex_line}\n",
        f"# 反馈流水（近 {fb_days} 天滚动窗；fb#编号 即证据引用口径）\n\n"
        + "\n".join(fb_lines),
    ])
