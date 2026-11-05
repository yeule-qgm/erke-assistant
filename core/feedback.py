"""L4 领域层：反馈解析与流水游标（关键 20%·反馈规则，ADR-0008）。

纯函数：回复文本 + 编号映射 + 当前时刻 → 反馈事件或错误提示语。
不碰网络、不碰文件——HTTP 壳在 feedback_server.py（L1），账本读写靠 store。

反馈词表：感兴趣 / 不感兴趣（"不感兴趣"优先匹配——它是"感兴趣"的超串）。
编号半角存取（mentions 的键为 "1".."10"），解析兼容：无空格（"1感兴趣"）、
圆圈数字（①-⑩）、全角数字（１）、汉字数字（一~十）——NFKC 归一 + 词表映射
（2026-10-01 健壮性实测补齐）。
反馈窗口 48 小时（mentions 记推送时刻）；同一活动反复反馈以最后一次为准
（流水按时间戳天然支持，无需去重逻辑）。

『撤销调档』口令（V3 06 票，ADR-0013 决定 10）：口令优先于编号反馈——撤销是
罕见且刻意的信号，而解释性文字里常夹反馈词（"撤销调档，最近推的都不感兴趣"），
反馈词优先会把口令错拆成"少了编号"的报错、撤销丢失。口令不进反馈流水
（登记的是回滚请求文件，归 feedback_server/store）。
"""
import re
import unicodedata
from datetime import datetime

FEEDBACK_WINDOW_HOURS = 48
POSITIVE, NEGATIVE, UNDO = "interest", "block", "undo"
NEG_WORDS, POS_WORDS, UNDO_WORDS = "不感兴趣", "感兴趣", "撤销调档"
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_NUM_WORDS = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
              "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def parse(text: str, mentions: dict, now: datetime,
          window_hours: int = FEEDBACK_WINDOW_HOURS) -> dict:
    """解析一条用户回复。

    text: 原始回复；mentions: {"1": {"activity_id","title","at"}}（巡检推送时写）。
    返回 {"ok": True, "activity_id", "title", "action"}
      或 {"ok": False, "reply": 给用户的提示语}——提示语由调用方原样回复，
      属"回复"而非"推送"，不占每日预算（2026-09-30 定稿）。
    『撤销调档』口令命中 → {"ok": True, "action": "undo"}（无活动映射，不进
    流水；由反馈服务登记回滚请求并即时回执，06 票）。
    """
    if _match_undo(text):
        return {"ok": True, "action": UNDO}
    action = _match_action(text)
    num = _match_number(text)
    if action is None and num is None:
        return _err("没看懂～格式：编号 + 感兴趣/不感兴趣（如『2 不感兴趣』）")
    if action is None:
        return _err("编号后面要带上反馈词：感兴趣 / 不感兴趣")
    if num is None:
        if len(mentions) == 1:  # 兜底：最近一条推送只有一件事时，纯词也认定是它
            num = next(iter(mentions))
        else:
            return _err("少了编号：请回复『编号 + 感兴趣/不感兴趣』")
    m = mentions.get(num)
    if m is None:
        return _err(f"编号 {num} 不在最近的推送里，请对最新一条推送回复")
    pushed = _dt(m.get("at"))
    if pushed and (now - pushed).total_seconds() > window_hours * 3600:
        return _err("这条推送已超过反馈窗口（48 小时），反馈作废了")
    return {"ok": True, "activity_id": m["activity_id"],
            "title": m.get("title", ""), "action": action}


def parse_card_action(value: dict, now: datetime,
                      window_hours: int = FEEDBACK_WINDOW_HOURS) -> dict:
    """解析卡片按钮回调的 action_value（V2c 双轨之一，ADR-0012）。

    value: 按钮携带的 {"kind","activity_id","title","action","at"}——activity_id
    直达，无需编号映射（编号机制只服务文本轨）；48h 窗口照管，窗口外点击给提示。
    返回形状与 parse 一致，落盘路径共用（feedback_server._record）。
    """
    if (value.get("kind") != "feedback" or not value.get("activity_id")
            or value.get("action") not in (POSITIVE, NEGATIVE)):
        return _err("按钮数据不完整，这次没记住——可回复『编号 感兴趣/不感兴趣』")
    pushed = _dt(value.get("at"))
    if pushed and (now - pushed).total_seconds() > window_hours * 3600:
        return _err("这条推送已超过反馈窗口（48 小时），反馈作废了")
    return {"ok": True, "activity_id": str(value["activity_id"]),
            "title": str(value.get("title", "")), "action": value["action"]}


def unprocessed(feedback_log: list[dict], cursor: int) -> list[dict]:
    """反馈流水中 seq 大于游标的新条目（巡检按此消费，游标存 reports）。"""
    return [e for e in feedback_log if e.get("seq", 0) > cursor]


def consume(feedback_log: list[dict], cursor: int) -> tuple[list[str], int]:
    """未消费流水 → (感兴趣活动 id 列表, 新游标)。
    不感兴趣由反馈服务直接写黑名单；"进关注"这一步归巡检执行（账本单一写者）。"""
    new = unprocessed(feedback_log, cursor)
    interest = [e["activity_id"] for e in new if e.get("action") == POSITIVE]
    return interest, max((e.get("seq", 0) for e in new), default=cursor)


def blocked(blocklist: dict) -> set:
    """黑名单账本 → 活动 id 集合（效力等同低分命运：永不再推）。"""
    return set(blocklist)


def _match_undo(text: str) -> bool:
    """『撤销调档』口令识别：NFKC 归一 + 去全部空白后子串匹配（"撤销 调档" 也认），
    与反馈词的子串匹配同款宽松度。"""
    collapsed = "".join(unicodedata.normalize("NFKC", text).split())
    return UNDO_WORDS in collapsed


def _match_action(text: str) -> str | None:
    if NEG_WORDS in text:
        return NEGATIVE
    if POS_WORDS in text:
        return POSITIVE
    return None


def _match_number(text: str) -> str | None:
    text = unicodedata.normalize("NFKC", text)  # 全角１→1、①→1、⑩→10
    for i, ch in enumerate(_CIRCLED):  # NFKC 后通常已转掉，留作保险
        if ch in text:
            return str(i + 1)
    for word, num in _NUM_WORDS.items():
        if word in text:
            return num
    m = re.search(r"\d{1,2}", text)  # 允许多位（如 ⑩→10），mentions 键上限 10
    return m.group() if m else None


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _err(reply: str) -> dict:
    return {"ok": False, "reply": reply}
