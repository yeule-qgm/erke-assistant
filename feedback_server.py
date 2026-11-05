"""L1 入口层：反馈接收服务（常驻）——飞书接收的 HTTP 壳。

职责只三样：验签 → 解析（core/feedback.py 纯函数）→ 落盘三件套 + 确认语回复。
规则与语义全部在 core/；本文件只做运输。

飞书接收：**长连接为主**——output/feishu_ws.py 的 WebSocket 客户端（lark-oapi
SDK）跑在本服务守护线程，免域名/免证书/免 443：im.message.receive_v1 → 文本轨
_ws_message（feedback.parse，顺带自动发现接收者 open_id）；card.action.trigger
→ 卡片轨 _ws_card（parse_card_action，3 秒内 toast 回执）。HTTP /feishu/ 路由
保留为回切公网回调时的备用路径（challenge 握手 + token 对 ERKE_FEISHU_VERIFY_
TOKEN，未配置该环境变量时放行并告警——仅回切后才有意义）。
落盘三件套：feedback.json 流水（seq 自增，巡检按游标消费；条目 join 当次
LLM/合成分，decision 账本缺失容错空值）；blocklist.json 增删（感兴趣=移除，
最后一次为准）；偏好库在场时旧档"反馈记录"追加区停用——证据只存流水一处，
缺席照旧追加（回退一致）。『撤销调档』口令：登记回滚请求文件 + 即时回执，
恢复归巡检下轮首步。确认语走飞书单聊——不占预算。
"""
import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import feedback
from output import feishu, feishu_ws
from store import (append_feedback_fact, load_json, load_params, load_profile,
                   register_rollback, save_json, update_blocklist)

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger("agent")
LOCK = threading.Lock()  # 反馈流水读改写互斥（单进程多线程足够）
PORT = int(os.environ.get("PORT", "80"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # 飞书握手是 POST，GET 一律拒
        self._reply(403, "forbidden")

    def do_POST(self):  # 用户消息/回调（飞书 JSON）
        if self.path.startswith("/feishu"):
            self._do_feishu()
            return
        self._reply(403, "forbidden")

    def _do_feishu(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        status, obj, after = _feishu_route(payload if isinstance(payload, dict) else {})
        self._reply_json(status, obj)
        if after:
            after()  # 确认语等慢动作放响应之后——飞书 3 秒时限纪律

    def log_message(self, *a):  # 压掉每请求一行的默认访问日志
        pass

    def _reply(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _feishu_route(payload: dict) -> tuple[int, dict, object]:
    """飞书回调分发（纯逻辑，便于单测）。返回 (HTTP 状态, 响应 JSON, 响应后回调)。"""
    if payload.get("type") == "url_verification":  # 保存回调地址的握手
        return 200, {"challenge": payload.get("challenge", "")}, None
    header = payload.get("header") or {}
    expect = os.environ.get("ERKE_FEISHU_VERIFY_TOKEN", "")
    got = payload.get("token") or header.get("token") or ""
    if expect and got != expect:
        LOG.warning(f"[反馈·飞书] token 校验不过，拒绝（event_type={header.get('event_type')}）")
        return 403, {"msg": "bad token"}, None
    if not expect:
        LOG.warning("[反馈·飞书] ERKE_FEISHU_VERIFY_TOKEN 未配置，暂放行（联调过渡期）")
    event = payload.get("event") or {}
    et = header.get("event_type", "")
    if et == "card.action.trigger":  # 卡片轨：3 秒内 toast 回执（与长连接共用核心）
        value = (event.get("action") or {}).get("value") or {}
        op = (event.get("operator") or {}).get("open_id", "feishu")
        ok, msg = _ws_card(value, op)
        return 200, {"toast": {"type": "success" if ok else "error",
                               "content": msg[:48]}}, None
    if et == "im.message.receive_v1":  # 文本轨：反馈解析入口
        msg = event.get("message") or {}
        open_id = ((event.get("sender") or {}).get("sender_id") or {}).get("open_id", "")
        if msg.get("message_type") != "text" or not open_id:
            return 200, {}, None
        try:
            text = json.loads(msg.get("content") or "{}").get("text", "")
        except ValueError:
            return 200, {}, None
        return 200, {}, (lambda: _ws_message(text, open_id))  # 先 200 再落盘回复
    return 200, {}, None  # 其余事件（已读回执等）：静默收下


def _ws_message(text: str, open_id: str) -> None:
    """飞书文本轨核心（长连接与 HTTP 路由共用）：发现接收者→解析落盘→回确认语。"""
    feishu.save_recipient(open_id)
    LOG.info(f"[反馈·飞书] 收到 {open_id[:8]}… 的回复：{text[:40]}")
    reply = _handle(open_id, text)
    ok, note = feishu.send_text(open_id, reply)
    if not ok:
        LOG.warning(f"[反馈·飞书] 确认语发送失败（不影响落盘）：{note}")


def _ws_card(value: dict, open_id: str) -> tuple[bool, str]:
    """飞书卡片轨核心（长连接与 HTTP 路由共用）：按钮 value → 反馈三件套。"""
    r = feedback.parse_card_action(
        value, datetime.now(),
        window_hours=load_params().get("feedback_window_hours", 48))
    if not r["ok"]:
        LOG.info(f"[反馈·飞书] 卡片未受理（{r['reply']}）")
        return False, r["reply"]
    return True, _record(r, open_id)


def _handle(fromuser: str, content: str) -> str:
    """文本轨：解析一条回复，落盘并返回确认/提示语。
    『撤销调档』口令（V3 06 票）：登记回滚请求文件 + 即时回执——不进反馈流水、
    不碰偏好库（写权限单写者，恢复归巡检下轮首步）；回执是回复语义，不占预算。"""
    cfg = load_params()
    r = feedback.parse(content, load_json("mentions.json", {}), datetime.now(),
                       window_hours=cfg.get("feedback_window_hours", 48))
    if not r["ok"]:
        LOG.info(f"[反馈] 未识别（{r['reply']}）")
        return r["reply"]
    if r["action"] == feedback.UNDO:
        register_rollback(note=content)
        LOG.info("[反馈] 『撤销调档』已登记，下轮巡检首步恢复最近快照")
        return "已登记，下轮巡检恢复（最近一次调档前的偏好）"
    return _record(r, fromuser)


def _record(r: dict, fromuser: str) -> str:
    """落盘三件套（流水/黑名单/偏好档案追加——末件带切换闸），返回确认语。
    卡片轨与文本轨共用。流水条目 join 当次 LLM 分（V3 06 票，spec 决策 14）：
    从 decision 账本（activities.json）取该活动最近一次打分的 llm/合成分，
    无记录或 theme/bootstrap/expired 等无分数条目 → None（缺失容错）。
    偏好档案追加闸（spec 决策 16/24）：偏好库在场（load_profile 非 None——
    含"损坏当未初始化"口径，与打分链换料同一判据）→ 证据只存流水一处；
    缺席 → 照旧追加旧档（回退一致）。"""
    with LOCK:
        now = datetime.now()
        log = load_json("feedback.json", [])
        decision = ((load_json("activities.json", {}).get(r["activity_id"]) or {})
                    .get("decision") or {})
        entry = {"seq": (log[-1]["seq"] + 1) if log else 1,
                 "time": now.isoformat(timespec="minutes"),
                 "activity_id": r["activity_id"], "title": r["title"],
                 "action": r["action"], "from": fromuser[:16],
                 "llm": decision.get("llm"), "score": decision.get("score")}
        log.append(entry)
        save_json("feedback.json", log)
        update_blocklist(r)
        if load_profile() is None:
            append_feedback_fact(r["title"], r["action"], entry["time"])
        LOG.info(f"[反馈] #{entry['seq']} 《{r['title'][:20]}》→ {r['action']}")
    if r["action"] == feedback.NEGATIVE:
        return f"已记住：《{r['title']}》不感兴趣，以后不再推它，同类也会少推。"
    return f"已记住：《{r['title']}》感兴趣，它的名额/截止/变动我会持续告诉你。"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if feishu_ws.enabled():
        threading.Thread(target=feishu_ws.run, daemon=True, name="feishu-ws",
                         kwargs={"on_message": _ws_message,
                                 "on_card": _ws_card}).start()
        LOG.info("[飞书·长连接] 接收线程已启动（文本轨+卡片轨）")
    elif feishu.configured():
        LOG.warning("[飞书·长连接] ERKE_FEISHU_* 已配置但 lark-oapi 未安装，"
                    "长连接未启动（pip install lark-oapi）")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info(f"反馈接收服务已启动，端口 {PORT}（飞书长连接 + HTTP 路由 /feishu/ 备用）")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
