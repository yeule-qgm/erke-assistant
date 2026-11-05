"""L4 领域层：系列子项目通道（关键 20%·新场次规则，ADR-0015）。

纯函数：子项目清单 + 活动账本 + 当前时刻 → 新场次事件。不碰网络、不碰文件。

立法要点（ADR-0015，2026-11-03 用户裁定）：
- 账本无此 id 的子项目即"新"；新且可行动（未开始且报名未截止）→ 新场次事件
  （series_new，优先级与变更同级）。首拉基线轮由此天然不刷屏：历史场次已开始/
  已截止不触发，"报名中且未开始"的存量场次破例推一次。
- 正当性继承母活动的关注——子项目不做主题词匹配、不进 LLM 打分池（发现通道
  唯一性）；黑名单/排除词母活动的新场次不推（接线与读侧过滤在 patrol ⑤.5/⑥.2）。
- once-only 键 {子项目id}:chird；有效期 = min(开始时刻, 报名截止)——报名截止后
  事件作废（陈闻不发，乒乓球事故同源）。
"""
from datetime import datetime

from core import develop


def new_children(children: list[dict], snapshots: dict) -> list[dict]:
    """账本无此 id 的子项目 = 新（基线判据，也是 once-only 的锚点）。"""
    return [c for c in children if c["id"] not in snapshots]


def actionable(child: dict, now: datetime) -> bool:
    """可行动 = 未开始且报名未截止（与 patrol ⑤ 主题必推的 _actionable 同口径）。"""
    start, dl = _dt(child.get("start_time")), _dt(child.get("deadline"))
    return (start is None or start > now) and (dl is None or dl > now)


def make_event(child: dict, parent_title: str, now_iso: str) -> dict:
    """新场次事件：正文带场次时间/报名截止/剩余名额，母活动名入理由。"""
    return develop.make_event(
        child, "series_new", _detail(child),
        reason=f"母活动『{parent_title}』新场次",
        key=f"{child['id']}:chird", now_iso=now_iso,
        valid_until=_valid_until(child))


def _detail(c: dict) -> str:
    parts = []
    start, dl = _dt(c.get("start_time")), _dt(c.get("deadline"))
    if start:
        parts.append(f"{start:%m-%d %H:%M} 开始")
    if dl:
        parts.append(f"报名至 {dl:%m-%d %H:%M}")
    if c.get("quota_left") is not None:
        parts.append("已满员" if c["quota_left"] == 0 else f"剩余 {c['quota_left']} 名")
    return "，".join(parts) or "新场次"


def _valid_until(c: dict) -> str | None:
    """有效期 = 开始与报名截止取较早者——两者都缺时返回 None（不设时限）。"""
    times = [t for t in (_dt(c.get("start_time")), _dt(c.get("deadline"))) if t]
    return min(times).isoformat(timespec="minutes") if times else None


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None
