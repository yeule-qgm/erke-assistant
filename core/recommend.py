"""L4 领域层：两级推荐（关键 20%·推荐提示词就在这里）。

第一级（ADR-0002）：主题命中 → 必推。确定性、零漏报、零成本——LLM 挂了它也照推。
第二级：其余活动交 LLM 按偏好档案打分；任何失败都返回空 dict 并说明原因，
调用方保持这些活动"待评分"，下次巡检自动重试（Q23：不丢、不乱标）。
"""
import json
import os
import re

# ---------- 关键 20%：推荐提示词（改这里 = 改推荐口味） ----------
SYSTEM_PROMPT = """你是我的第二课堂活动筛选助手。我会给你我的偏好档案、我的主题词表和候选活动列表，请给每个活动打 1~10 分：
- 8~10 分：正中偏好核心，值得专门推荐（与主题词表同族或相关的活动一律视为高匹配）
- 5~7 分：与偏好沾边，知道即可
- 1~4 分：与偏好无关
只输出一个 JSON 对象，不要输出任何其他文字或代码围栏，格式：
{"results": [{"id": "活动id", "score": 8, "reason": "不超过 20 字的推荐理由"}]}
每个候选活动都必须出现在 results 里，一个都不能少。"""

BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"


def theme_matches(activity: dict, themes: list[str]) -> bool:
    """主题=子串匹配标题+简介（架构文档工程辖区：含简介全文）。"""
    text = activity.get("title", "") + " " + activity.get("desc", "")
    return any(t and t in text for t in themes)


def excluded(activity: dict, excludes: list[str]) -> bool:
    """标题或简介命中任一排除词 → 一律不推荐（ADR-0011，压倒主题命中）。
    精确子串，与主题词同款；语义变体由 LLM 读档案"排除词："行兜底。"""
    text = activity.get("title", "") + " " + activity.get("desc", "")
    return any(x and x in text for x in excludes)


def split_by_theme(activities: list[dict], themes: list[str]):
    hits, rest = [], []
    for a in activities:
        (hits if theme_matches(a, themes) else rest).append(a)
    return hits, rest


def llm_score(candidates: list[dict], prefs_text: str, llm: dict,
              themes: list[str] | None = None) -> tuple[dict, str]:
    """按偏好打分（Q27-b：主题词表随偏好段注入，同族语义由模型掌握）。
    prefs_text = 偏好段成品文本（回退模式 = 旧档案全文；偏好库模式 =
    curate.render_scoring_prefs 渲染——spec 决策 1，本模块不感知偏好库结构）。
    llm = {"model": 模型名, "base_url": 兼容端点}——V2b 起端点可换（DeepSeek 直替）。
    返回 ({活动id: {"score","reason"}}, 降级原因)。失败不抛异常。"""
    if not candidates:
        return {}, ""
    # 激活判断只看正文（剔除 # 注释与 > 引言行）——模板引言里也含"待填"二字，
    # 不能只看前 80 字符（2026-09-22 实测：用户填好正文但保留模板头，打分被永久挂起）
    body = "\n".join(ln for ln in prefs_text.splitlines()
                     if not ln.strip().startswith(("#", ">")))
    if not body.strip() or "待填" in body:
        return {}, "偏好档案还没填写（骨架期正常）——保持待评分"
    key = os.environ.get("ERKE_LLM_API_KEY")
    if not key:
        return {}, "未设置 ERKE_LLM_API_KEY——保持待评分"
    try:  # import 放函数内：没装 openai 也不影响其余功能
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=(llm or {}).get("base_url", BASE_URL))
        reply = client.chat.completions.create(
            model=(llm or {}).get("model", "glm-4-flash"),
            temperature=0.2,  # 打分要稳定不要发散（学习记录 0004）
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_content(prefs_text, candidates, themes or [])},
            ],
        )
        text = reply.choices[0].message.content
        scores, why = _parse_results(text, {a["id"] for a in candidates})
        if why:
            return {}, why
        return scores, ""
    except Exception as e:  # 网络/解析/格式，一律降级（Q23）
        return {}, f"LLM 打分失败：{e!r}——保持待评分"


def _parse_results(text: str, valid_ids: set[str]) -> tuple[dict, str]:
    """容错解析模型返回的打分 JSON。

    glm-4-flash 偶尔在 reason 里吐未转义引号，整包 JSON 非法（2026-09-22 实测
    JSONDecodeError char 1887）。策略：先整包解析；失败则逐 {...} 片段抢救——
    能抢回几个算几个，解析不了的保持待评分下轮重试（Q23：坏时少丢事）。
    """
    text = _strip_fence(text)
    try:
        return _collect(json.loads(text).get("results"), valid_ids), ""
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    rescued: dict[str, dict] = {}
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            one = _collect([json.loads(m.group(0))], valid_ids)
        except json.JSONDecodeError:
            continue
        rescued.update(one)
    if rescued:
        return rescued, ""
    return {}, "无法从模型返回中解析出任何打分结果"


def _collect(results, valid_ids: set[str]) -> dict[str, dict]:
    """从 results 列表收集合法打分：id 必须在候选内，分数钳制 1~10。"""
    out: dict[str, dict] = {}
    for r in results or []:
        if not isinstance(r, dict) or str(r.get("id")) not in valid_ids:
            continue  # 幻觉 id 直接丢弃
        try:
            out[str(r["id"])] = {"score": max(1, min(10, int(r["score"]))),
                                 "reason": str(r.get("reason", ""))[:60]}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _user_content(prefs_text: str, candidates: list[dict], themes: list[str]) -> str:
    theme_part = ("# 我的主题词表（命中这些词的活动本来就会必推；"
                  "与它们同族或相关的也请给高分）\n\n" + "、".join(themes) + "\n\n"
                  if themes else "")
    return (f"# 我的偏好档案\n\n{prefs_text}\n\n"
            f"{theme_part}"
            f"# 候选活动（共 {len(candidates)} 个）\n\n"
            + json.dumps(candidates, ensure_ascii=False))


def _strip_fence(text: str) -> str:
    """模型偶尔在 JSON 外裹 ```json 围栏或说明文字，掐头去尾只留花括号之间
    （不赌端点的 response_format——学习记录 0005 的教训）。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]
