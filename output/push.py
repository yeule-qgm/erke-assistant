"""L5 输出层：推送通道选择与重试语义（关键 20%·推送调用，接缝①签名不变）。

通道链：飞书消息卡片（主——单聊随时可发，推荐行带反馈按钮）→ 未配置仅打印。
飞书分支开关=两个 ERKE_FEISHU_* 密钥齐备；都不配则推送降级为终端打印（开发机/
离线演练），无需改代码。失败语义（Q23）：退避重试 3 次仍败 → ok=False 回待发
队列——事件永不丢。2026-09-30：参加提醒即时推送从 patrol 下沉至此。
"""
import logging

from output import feishu
from output import format as fmt

LOG = logging.getLogger("agent")


def send_markdown(title: str, body: str, events: list | None = None,
                  note: str = "") -> tuple[bool, str]:
    """推一条汇总消息。events（可选）=本条消息的事件列表，飞书分支据此排卡片按钮；
    note（可选）= 汇总尾部附注行（V3 05 票"今日档案调整"，spec 决策 10——不占
    推送条数）。返回 (是否算送达, 说明)。"""
    if feishu.configured():
        ok, err = feishu.send_card(title, body, events, note)
        if ok:
            return True, "飞书消息卡片"
        return False, f"飞书发送失败：{err}"
    bar = "=" * 44
    print(f"\n{bar}\n【未配置推送通道，仅打印】{title}\n{bar}\n{body}")
    return True, "通道未配置，仅打印"


def send_attend_now(urgent: list[dict], now, dry_run: bool) -> tuple[list[dict], list[str]]:
    """参加提醒即时推送（时间敏感，不受时点/预算限制）。返回 (回队事件, 已发事件键)。"""
    requeue: list[dict] = []
    sent_keys: list[str] = []
    for e in urgent:
        title, body, _, _ = fmt.render([e], now)
        if dry_run:
            print(f"\n【演练·不真发】{title}\n{body}\n")
            LOG.info(f"[演练] 将即时推送：{title}")
            continue
        ok, note = send_markdown(title, body)
        if ok:
            if e.get("key"):
                sent_keys.append(e["key"])
            LOG.info(f"[推送] {title}（参加提醒，即时）")
        else:
            LOG.error(f"[推送] 失败：{note}；事件回待发队列")
            requeue.append(e)
    return requeue, sent_keys
