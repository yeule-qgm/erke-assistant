"""L4 领域层：动向检测（关键 20%·动向触发规则）。

纯函数：旧快照 + 新数据 + 当前时刻 → 事件列表。不碰网络、不碰文件、不记日志。
五种动向只对关注清单产生（用户主动加的约束）；活动开始时间已过则不再产生
截止/参加/名额动向——提醒一个已经开始的活动没有意义（Q22）。

长期活动内容动向（ADR-0014，2026-10-20 用户裁定）：theme 来源的活动即使
已开始，仍跑变更比对（含标题/简介）——周常类母活动一挂一学期，首场时间
永远在过去，若按"已开始即沉寂"处理，平台上的更新对用户永远不可见。
其余来源（signup/rec/interest/llm）维持 Q22 原判。

每个事件都带 valid_until（到期时刻）：入队等待期间过期的事件，出队复查时丢弃
（budget.plan）——事件永不丢 ≠ 陈闻也要发（2026-09-28 乒乓球观赛事故，2026-09-29）。
"""
import difflib
from datetime import datetime, timedelta

# 事件优先级（PROJECT 2026-09-20；新场次与变更同级，ADR-0015——场次通常 3~5 天内
# 开场，紧急度接近截止类）：截止 > 变更=新场次 > 参加提醒 > 名额；新推荐=5 在 recommend 侧
PRIORITY = {"deadline": 1, "change": 2, "series_new": 2, "attend": 3, "quota": 4}


def make_event(activity: dict, type_: str, detail: str, *, priority=None,
               reason: str = "", key: str | None = None, now_iso: str = "",
               valid_until: str | None = None) -> dict:
    return {
        "type": type_,
        "activity_id": activity["id"],
        "title": activity["title"],
        "detail": detail,
        "priority": PRIORITY.get(type_, 5) if priority is None else priority,
        "reason": reason,
        "key": key,            # once-only 事件的登记键（deadline/attend/quota 档位）
        "at": now_iso,
        "valid_until": valid_until,  # 事件有效期（入队期间过期则丢弃，防陈闻）
    }


def detect(watch: dict, snapshots: dict, by_id: dict, now: datetime,
           params: dict, reported: set) -> list[dict]:
    """比对关注活动的旧快照与新数据，产出动向事件。"""
    events = []
    for aid, entry in watch.items():
        a = by_id.get(aid)
        if a is None:
            # 已下架：截止/名额/变更需要新数据可比，无米下锅；但参加提醒只需要
            # 开始时间——由快照兜底驱动（ADR-0010：列表只回"报名中"，截止即下架，
            # 而提醒窗开在开始前 60min，两头扑空曾致整月 attend 零触发）
            snap_a = (snapshots.get(aid) or {}).get("activity") or {}
            if snap_a and "signup" in entry.get("sources", []):
                events += _attend(snap_a, _dt(snap_a.get("start_time")),
                                  now, params, reported)
            continue  # 其余动向：没有可比对的新数据
        start = _dt(a.get("start_time"))
        started = bool(start and start <= now)
        theme = "theme" in entry.get("sources", [])
        if started and not theme:
            continue  # 已开始/已结束：停止产生动向（Q22）；theme 来源见 ADR-0014
        if not started:
            # 截止提醒：进入提前窗口且从未报告过（once-only）
            events += _deadline(a, _dt(a.get("deadline")), now, params, reported)
            # 参加提醒：报名过的活动（待参加），开始前 attend_lead_minutes 内提醒一次
            if "signup" in entry.get("sources", []):
                events += _attend(a, start, now, params, reported)
        # 快照比对：变更字段（theme 来源含标题/简介，ADR-0014）+ 名额档位
        # （名额只对未开始的产生——已开始活动名额信息失去"还来得及报名"的意义）
        snap = snapshots.get(aid)
        if snap:
            old = snap.get("activity", {})
            events += _diff(old, a, params, reported, content=theme, now=now)
            if not started:
                events += _quota(old, a, params, reported)
    return events


def _deadline(a, dl, now, params, reported):
    if not dl or dl <= now:
        return []
    if (dl - now).total_seconds() > params["deadline_lead_hours"] * 3600:
        return []
    if f"{a['id']}:deadline" in reported:
        return []
    detail = f"报名将于 {_fmt(dl)} 截止"
    start = _dt(a.get("start_time"))
    if start and start > now:
        detail += f"，{_fmt(start)} 开始"  # 顺带带上开始时间（2026-10-18 用户裁定）
    return [make_event(a, "deadline", detail,
                       key=f"{a['id']}:deadline", valid_until=dl.isoformat(timespec="minutes"))]


def _attend(a, start, now, params, reported):
    if not start or start <= now:
        return []  # 没有开始时间，或已开始（已开始不补发）
    minutes = (start - now).total_seconds() / 60
    if minutes > params["attend_lead_minutes"]:
        return []
    if f"{a['id']}:attend" in reported:
        return []
    return [make_event(a, "attend", f"{round(minutes)} 分钟后开始，记得参加",
                       key=f"{a['id']}:attend", valid_until=start.isoformat(timespec="minutes"))]


def _diff(old: dict, new: dict, params: dict, reported: set,
          content: bool = False, now: datetime | None = None) -> list[dict]:
    """快照字段比对 → 变更事件。content=True 时加比对标题/简介（ADR-0014，
    theme 来源的内容动向）：简介变更只报新增文本的摘录，不整段复读。
    reported 形参保留自旧签名（变更类无 once-only 键，实际未用）。"""
    events = []
    fields = [("start_time", "时间"), ("location", "地点"), ("status", "状态")]
    if content:
        fields += [("title", "标题"), ("desc", "简介")]
    for field, label in fields:
        ov, nv = old.get(field), new.get(field)
        if not (ov and nv and str(ov) != str(nv)):
            continue
        if field == "desc":
            detail = f"简介更新：{_excerpt(str(ov), str(nv))}"
        elif field == "title":
            detail = f"标题变更：{str(ov)[:40]} → {str(nv)[:40]}"  # 片名常在标题里，放宽到 40
        else:
            detail = f"{label}变更：{_short(ov)} → {_short(nv)}"
        events.append(make_event(new, "change", detail,
                                 valid_until=_valid_for(new, now, params)))
    return events


def _valid_for(a: dict, now: datetime | None, params: dict) -> str | None:
    """变更事件有效期：活动未开始 → 开始时刻（开演后的时间地点变更无人关心）；
    已开始（长期活动内容动向）→ now + change_ttl_hours。旧规则拿开始时刻当
    有效期，对已开始的活动等于事件一生成就过期，永远发不出去——TTL 是
    ADR-0014 的前置立法，过期不补发（陈闻不发原则）。"""
    st = _dt(a.get("start_time"))
    if now is None or st is None or st > now:
        return st.isoformat(timespec="minutes") if st else None
    ttl = params.get("change_ttl_hours", 24)
    return (now + timedelta(hours=ttl)).isoformat(timespec="minutes")


def _excerpt(old: str, new: str, cap: int = 80) -> str:
    """简介变更的新增文本摘录：字符级 diff 取 insert/replace 的新侧片段拼接、
    截断；纯删改（无新增文本）退回新文头部——总得让用户知道改动的方向。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added = "".join(new[b1:b2] for tag, _, _, b1, b2 in sm.get_opcodes()
                    if tag in ("insert", "replace"))
    text = added.strip() or new[:cap]
    return text[:cap]


def _quota(old: dict, new: dict, params: dict, reported: set) -> list[dict]:
    prev, cur = old.get("quota_left"), new.get("quota_left")
    if prev is None or cur is None or prev == cur:
        return []
    t = params["quota_threshold"]
    tier, detail = None, ""
    if cur == 0:                                   # 档位最高：报满
        tier, detail = "full", "已报满"
    elif prev == 0 and cur > 0:                    # 满员后出现空位
        tier, detail = "vacancy", f"出现空位（剩余 {cur}）"
    elif prev >= t and 0 < cur < t:                # 首次跌破阈值
        tier, detail = "below", f"剩余名额跌破 {t}（剩 {cur}）"
    if tier is None or f"{new['id']}:quota:{tier}" in reported:
        return []  # 同档只报一次；同轮多名只取最高档（上面 if/elif 顺序即档位顺序）
    return [make_event(new, "quota", detail, key=f"{new['id']}:quota:{tier}",
                       valid_until=_reg_deadline_of(new))]


def _reg_deadline_of(a: dict) -> str | None:
    """名额类事件的有效期 = 报名截止与开始时间取较早者——名额信息只在
    "还来得及报名"时有意义（乒乓球观赛事故：活动开演后 23 点才推"已报满"）。"""
    times = [t for t in (_dt(a.get("deadline")), _dt(a.get("start_time"))) if t]
    return min(times).isoformat(timespec="minutes") if times else None


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _fmt(d: datetime) -> str:
    return d.strftime("%m-%d %H:%M")


def _short(v) -> str:
    return str(v)[:16]
