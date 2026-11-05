"""L5 输出层：把事件列表排成一条纯文本汇总消息（Q14 定稿：五种标签、不带链接）。

2026-09-22：返回实际装下的事件（included）——超出的由调用方退回待发队列，
绝不截断丢失（登记与发送必须一致）。
2026-09-30（V2a）：额外返回编号映射（半角 "1".."9" → 事件），推送成功后由
调用方写入 state/mentions.json——用户回复"编号+感兴趣/不感兴趣"的指认依据（ADR-0008）。
2026-10-18（V2c）：主通道为飞书消息卡片，上限 3900 留余量；
超长整条顺延机制不变（装不下的整条退回待发队列，绝不截断丢失）。
build_card 同时迁入（排版层适配通道，ADR-0007 先例）：纯函数，飞书卡片 JSON。
"""

LABELS = {"deadline": "截止", "change": "变更", "attend": "参加提醒",
          "quota": "名额", "theme": "推荐", "recommend": "推荐",
          "series_new": "新场次"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
MAX_BYTES = 3900  # 飞书卡片上限宽裕，留 JSON 转义余量（ADR-0012）；超长顺延照旧
BTN_TYPES = {"interest": "primary", "block": "danger"}  # 感兴趣=蓝 不感兴趣=红


def render(events: list[dict], now, note: str = "") -> tuple[str, str, list[dict], dict]:
    """事件（已按优先级排好）→ (标题, 纯文本正文, 装下的事件, 编号映射)。
    note（可选）= 汇总尾部附注行（V3 05 票"今日档案调整"，spec 决策 10）：
    不是事件——不占编号、不进 included/numbers、不计推送条数；装不下时
    整行丢弃（附注是透明性赠品，绝不为此挤掉事件）。"""
    title = f"二课助手 {now:%m-%d %H:%M} · {len(events)} 件事"
    lines, included, numbers = [], [], {}
    size = len(title.encode("utf-8")) + 32
    for e in events:
        no = CIRCLED[len(included)] if len(included) < len(CIRCLED) else f"{len(included) + 1}."
        reason = f"——{e['reason']}" if e.get("reason") else ""
        line = (f"{no} [{LABELS.get(e['type'], e['type'])}] "
                f"『{e['title']}』{e['detail']}{reason}")
        size += len(line.encode("utf-8")) + 1
        if size > MAX_BYTES and included:
            break  # 至少装一条；装不下的留给调用方顺延
        lines.append(line)
        included.append(e)
        numbers[str(len(included))] = e
    if note and size + len(note.encode("utf-8")) + 1 <= MAX_BYTES:
        lines.append(note)
    return title, "\n".join(lines), included, numbers


def build_card(title: str, body: str, events: list, note: str = "") -> dict:
    """纯函数：标题+正文行 → 飞书消息卡片 JSON（V2c，ADR-0012）。render 保证
    第 i 行对应 events[i]；带 activity_id 的行全挂两按钮——含动向行（修正案·丁，
    2026-10-20：与文本编号轨对齐，动向反馈=合法偏好事实），按钮 value 自带
    activity_id/title/at——点击直达 parse_card_action，无需编号。note = 尾部
    附注行（V3 05 票）：不与事件行 zip（附注不是事件），独立成 div 放在按钮
    提示语之前。"""
    lines = body.split("\n")
    elements = []
    for line, e in zip(lines, events):
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": line or " "}})
        if e.get("activity_id"):
            elements.append({"tag": "action", "actions": [
                {"tag": "button", "type": t,
                 "text": {"tag": "plain_text", "content": w},
                 "value": {"kind": "feedback", "activity_id": e["activity_id"],
                           "title": str(e.get("title", ""))[:40],
                           "action": a, "at": e.get("at", "")}}
                for a, w, t in (("interest", "感兴趣", BTN_TYPES["interest"]),
                                ("block", "不感兴趣", BTN_TYPES["block"]))]})
    if note:
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": note}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {
        "tag": "lark_md", "content": "点按钮或回复『编号 感兴趣/不感兴趣』皆可（48h 内有效）"}})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue",
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}
