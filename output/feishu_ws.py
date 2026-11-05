"""L5 输出层：飞书长连接接收端（V2c 修正案·丙，ADR-0012）。

2026-10-18 立法选公网回调；实施中实锤飞书回调强制 HTTPS+受信证书、用户无域名
且免费域名不稳（用户裁定）→ 启动 ADR-0012 后备条款切长连接：官方 SDK 的
WebSocket 客户端免域名/免证书/免 443，鉴权用 app 凭证自带（无需
ERKE_FEISHU_VERIFY_TOKEN），auto_reconnect 默认开、进程级故障归 systemd。
本模块只做运输：消息事件→on_message(text, open_id)；卡片回调→on_card(value,
open_id)→toast 应答（3 秒时限）。解析与落盘在 feedback_server/core（接缝⑤）。
HTTP /feishu/ 路由保留（回切公网回调的现成路径，当前闲置）。SDK 未装的
开发机：enabled() 为假，反馈服务照常起（长连接接收不启用）。
"""
import json
import logging
import os

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger, P2CardActionTriggerResponse)
except ImportError:  # noqa: E401 依赖见 requirements.txt（ADR-0012 修正案）
    lark = None

LOG = logging.getLogger("agent")


def enabled() -> bool:
    """密钥齐 + SDK 在场才启动长连接。"""
    return (lark is not None
            and bool(os.environ.get("ERKE_FEISHU_APP_ID")
                     and os.environ.get("ERKE_FEISHU_APP_SECRET")))


def run(on_message, on_card) -> None:
    """阻塞式长连接主体（feedback_server 的守护线程里跑）。"""
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(
                   lambda d: _recv_message(d, on_message))
               .register_p2_card_action_trigger(
                   lambda d: _recv_card(d, on_card))
               .build())
    client = lark.ws.Client(os.environ["ERKE_FEISHU_APP_ID"],
                            os.environ["ERKE_FEISHU_APP_SECRET"],
                            event_handler=handler, log_level=lark.LogLevel.INFO)
    LOG.info("[飞书·长连接] 启动（自动重连开，文本轨+卡片轨已注册）")
    client.start()  # 阻塞运行；断线重连归 SDK，进程崩溃归 systemd


def _recv_message(data: P2ImMessageReceiveV1, on_message) -> None:
    try:
        ev = data.event
        if not ev or not ev.message or ev.message.message_type != "text":
            return  # 表情/图片/已读回执等：收下不处理
        open_id = ((ev.sender.sender_id.open_id) if ev.sender and ev.sender.sender_id else "")
        if open_id:
            text = json.loads(ev.message.content or "{}").get("text", "")
            on_message(text, open_id)
    except Exception:  # 回调里绝不让异常逃逸——SDK 会当处理器故障
        LOG.exception("[飞书·长连接] 消息事件处理异常")


def _recv_card(data: P2CardActionTrigger,
               on_card) -> P2CardActionTriggerResponse:
    try:
        ev = data.event
        value = (ev.action.value or {}) if ev and ev.action else {}
        op = ((ev.operator.open_id or "feishu")
              if ev and ev.operator and ev.operator.open_id else "feishu")
        ok, msg = on_card(value, op)
        return P2CardActionTriggerResponse(
            {"toast": {"type": "success" if ok else "error", "content": msg[:48]}})
    except Exception:
        LOG.exception("[飞书·长连接] 卡片回调处理异常")
        return P2CardActionTriggerResponse(
            {"toast": {"type": "error", "content": "处理异常，这次没记住"}})
