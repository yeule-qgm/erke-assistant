"""L4 领域层：关键词加权评分（V2b，PROJECT 加权合成评分节）。

关键词分 = 生效词表在 标题(×2)/简介(×1) 的命中数，封顶 10——纯字面信号。
词表来源（04 票起由调用方传入，本模块不感知偏好库结构，spec 决策 1）：偏好库
模式 = curate.effective_keywords 出并集（种子 ∪ 活数组）；回退模式 =
extract_keywords 解析旧档案行。合成（加法式，2026-10-01 修正）：
score = LLM分 + w×关键词分，封顶 10——词表零命中时合成分 = LLM 分（保持旧
校准），命中强才加分（兜底语义）。
曾按加权平均实现，验收数据暴露：关键词稀疏命中下 0.7×LLM 使 8 分活动也过不了
7 分线（全量抑制回归），故改回 Q28 原始加法语义。
decision 分列存 llm/keyword/score（数据接缝：V3 调参直接读三列，无需迁移）。
全程无 ML——权重手工调；V3 才用 feedback.json 拟合权重。
"""
import re


def _pref_words(prefs_text: str, prefix: str) -> list[str]:
    """档案里以 prefix 开头的行的词表（、，, 与空白分隔；去重）。
    注释行（#/>）与"## 反馈记录"章节之后的内容一律忽略。"""
    words: list[str] = []
    for ln in prefs_text.splitlines():
        s = ln.strip()
        if s.startswith("## 反馈记录"):
            break
        if not s or s.startswith(("#", ">")) or not s.startswith(prefix):
            continue
        for w in re.split(r"[、，,\s]+", s.split("：", 1)[-1].split(":", 1)[-1].strip()):
            w = w.strip()
            if w and w not in words:
                words.append(w)
    return words


def extract_keywords(prefs_text: str) -> list[str]:
    """偏好档案里所有"关键词："行的词表（V2b 加权评分用）。"""
    return _pref_words(prefs_text, "关键词")


def extract_excludes(prefs_text: str) -> list[str]:
    """偏好档案"排除词："行的词表——硬排除词表（ADR-0011），与关键词行同款规则。"""
    return _pref_words(prefs_text, "排除词")


def keyword_score(activity: dict, keywords: list[str]) -> int:
    """标题命中一个词 = 2 分，简介 = 1 分，累计封顶 10。"""
    title, desc = activity.get("title", ""), activity.get("desc", "")
    score = 0
    for w in keywords:
        if w in title:
            score += 2
        elif w in desc:
            score += 1
        if score >= 10:
            return 10
    return score


def combine(llm: int, keyword: int, weight: float) -> float:
    """合成分 = LLM分 + 权重×关键词分，封顶 10，保留一位小数。
    加法式：关键词零命中时合成分 = LLM 分（校准不变），命中强才加分（兜底）。"""
    return round(min(10, max(1, llm + weight * keyword)), 1)


def enrich(scores: dict, candidates: list[dict], keywords: list[str],
           weight: float) -> dict:
    """给 llm_score 的结果补关键词分并合成，分列返回：
    {id: {"score"(合成), "llm", "keyword", "reason"}}——调用方阈值判断与
    decision 落盘全部用这个分列结构（数据接缝，V3 不迁移）。
    收生效词表而非档案文本（04 票，spec 决策 1：词表提取从偏好库 JSON 取）：
    偏好库模式调用方传 curate.effective_keywords(偏好库)，回退模式传
    extract_keywords(旧档全文)——提取的分支归调用方，本模块只管算分。"""
    by_id = {a["id"]: a for a in candidates}
    out = {}
    for aid, s in scores.items():
        kw = keyword_score(by_id[aid], keywords) if aid in by_id else 0
        out[aid] = {"score": combine(s["score"], kw, weight),
                    "llm": s["score"], "keyword": kw,
                    "reason": s.get("reason", "")}
    return out
