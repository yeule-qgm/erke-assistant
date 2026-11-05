"""L4 领域层：预算仲裁——固定推送时点、预算、待发队列（关键 20%）。

规则（PROJECT 2026-09-22 + 2026-09-28）：
- 推送时点：deliver_hours 内，或当天第一次有内容的巡检（≈开机补跑那次，且不得早于
  first_push_min_hour——凌晨不开波，事件等到早上首波或固定时点）；其余巡检只入队
- 出队复查：入队期间失效的事件（截止已过/活动已开始）丢弃并记日志，不发陈闻
- 预算：自然日零点重置；当日已发满 daily_budget 时事件入队（不丢事件，Q23）
- 参加提醒（attend）不进本仲裁——时间敏感，由入口层即时推送
"""
from datetime import datetime


def plan(events: list[dict], queue: list[dict], reports: dict, now,
         params: dict) -> tuple[list[dict], list[dict], list[str]]:
    """决定本巡检发什么、留什么。返回 (待发送事件, 新队列, 说明行)。

    调用方约定：待发送列表交给推送层；推送失败/排版装不下的事件并回新队列。
    """
    notes: list[str] = []
    hours = params.get("deliver_hours", [12, 23])
    min_hour = params.get("first_push_min_hour", 0)
    delivered_today = reports.get("last_delivery_date") == _today(now)
    first_wave = not delivered_today and now.hour >= min_hour  # 每天第一波内容也推送
    deliver = now.hour in hours or first_wave
    if not deliver:
        if events:
            notes.append(f"非推送时点：{len(events)} 条事件入待发队列"
                         f"（时点：每日首波 ≥{min_hour} 点 + {hours} 点）")
        return [], list(queue) + events, notes

    merged = list(queue) + events
    if not merged:
        return [], [], notes

    # 出队复查：入队期间失效的丢弃（防陈闻）
    fresh = []
    for e in merged:
        vu = e.get("valid_until")
        if vu:
            try:
                if datetime.fromisoformat(vu) <= now:
                    notes.append(f"丢弃过期事件：『{e.get('title', '')[:14]}』{e.get('type')}")
                    continue
            except ValueError:
                pass
        fresh.append(e)
    merged = fresh
    if not merged:
        return [], [], notes

    sent_today = reports.get("sent", 0) if reports.get("date") == _today(now) else 0
    if sent_today >= params["daily_budget"]:
        notes.append(f"今日预算已用尽（{sent_today}/{params['daily_budget']}）："
                     f"{len(merged)} 条事件入队，下个推送时点合并发送")
        return [], merged, notes

    merged.sort(key=lambda e: (e.get("priority", 9), e.get("at", "")))
    reports["last_delivery_date"] = _today(now)  # 本时点确实推送了内容，记一天一次的"首波"
    if queue:
        notes.append(f"待发队列清空：含隔夜 {len(queue)} 条，本条共 {len(merged)} 件事")
    return merged, [], notes


def register(reports: dict, events: list[dict], now) -> None:
    """推送成功后记账：当日预算 +1；once-only 事件登记，防重报（验收线之一）。"""
    if reports.get("date") != _today(now):
        reports["date"] = _today(now)
        reports["sent"] = 0
    reports["sent"] = reports.get("sent", 0) + 1
    registry = reports.setdefault("events", {})
    for e in events:
        if e.get("key"):
            registry[e["key"]] = now.isoformat(timespec="minutes")


def _today(now) -> str:
    return now.strftime("%Y-%m-%d")
