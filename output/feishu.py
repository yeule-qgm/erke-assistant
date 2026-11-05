"""L5 输出层：飞书自建应用运输细节（V2c，ADR-0012 通道实现）。

tenant_access_token（约 2h，缓存 state/feishu_token.json）、
单聊发消息（文本/消息卡片）。卡片 JSON 构建在 output/format.py（排版层适配
通道，ADR-0007 先例）；上层的通道选择与重试语义在 push.py（接缝①）。
密钥走环境变量 ERKE_FEISHU_APP_ID / ERKE_FEISHU_APP_SECRET；接收者 open_id
由反馈服务从首条消息事件自动发现并存同一缓存（config [feishu] open_id 可固定）。
"""
import json
import logging
import os
import time

import requests

from core import retry
from output import format as fmt
import store

LOG = logging.getLogger("agent")
_API = "https://open.feishu.cn/open-apis"


def configured() -> bool:
    """飞书分支的开关：两个密钥齐了才启用；都不配则推送降级为仅打印（开发机/离线演练）。"""
    return bool(os.environ.get("ERKE_FEISHU_APP_ID")
                and os.environ.get("ERKE_FEISHU_APP_SECRET"))


def tenant_token() -> str | None:
    """tenant_access_token，带缓存（expire 前提前 300s 刷新），失败返回 None。"""
    cache = _load_cache()
    if cache.get("expire_at", 0) > time.time():
        return cache.get("token")
    try:
        r = requests.post(f"{_API}/auth/v3/tenant_access_token/internal", json={
            "app_id": os.environ.get("ERKE_FEISHU_APP_ID"),
            "app_secret": os.environ.get("ERKE_FEISHU_APP_SECRET")}, timeout=10)
        data = r.json()
        if data.get("code") != 0:
            LOG.error(f"[飞书] 取 tenant_access_token 失败：{data}")
            return None
    except requests.RequestException as e:
        LOG.error(f"[飞书] 取 tenant_access_token 网络失败：{e!r}")
        return None
    _save_cache({**cache, "token": data["tenant_access_token"],
                 "expire_at": time.time() + data.get("expire", 7200) - 300})
    return data["tenant_access_token"]


def recipient() -> str | None:
    """接收者 open_id：config 固定值 > 事件自动发现缓存；都没有则本分支不可用。"""
    fixed = store.load_config("feishu").get("open_id", "")
    if fixed:
        return fixed
    return _load_cache().get("open_id")


def save_recipient(open_id: str) -> None:
    """反馈服务发现 open_id 后落缓存（巡检进程经文件共享，状态全在文件）。"""
    _save_cache({**_load_cache(), "open_id": open_id})


def send_card(title: str, body: str, events: list | None = None,
              note: str = "") -> tuple[bool, str]:
    """发单聊消息：有事件列表→消息卡片（推荐行带按钮），无→纯文本（告警/参加提醒）。
    note = 汇总尾部附注行（V3 05 票"今日档案调整"），只进卡片（纯文本分支的
    body 已含它——render 负责拼进正文）。"""
    token, who = tenant_token(), recipient()
    if not token:
        return False, "tenant_access_token 不可用"
    if not who:
        return False, "接收者 open_id 未知（先给机器人发条消息）"
    if events:
        content = fmt.build_card(title, body, events, note)
        msg_type = "interactive"
    else:
        content = {"text": f"{title}\n{body}"}
        msg_type = "text"
    payload = {"receive_id": who, "msg_type": msg_type,
               "content": json.dumps(content, ensure_ascii=False)}

    def attempt():
        try:
            r = requests.post(f"{_API}/im/v1/messages", params={"receive_id_type": "open_id"},
                              json=payload, timeout=10,
                              headers={"Authorization": f"Bearer {token}"})
            data = r.json()
            if data.get("code") == 0:
                return True
            raise retry.AttemptFailed(f"code={data.get('code')} {str(data.get('msg'))[:80]}")
        except requests.RequestException as e:
            raise retry.AttemptFailed(repr(e))

    ok, err = retry.with_retry(attempt)
    return (True, "") if ok else (False, err)


def send_text(open_id: str, text: str) -> tuple[bool, str]:
    """反馈确认语等单聊文本（不占推送预算），best-effort。"""
    token = tenant_token()
    if not token or not open_id:
        return False, "token/open_id 不可用"
    try:
        r = requests.post(f"{_API}/im/v1/messages", params={"receive_id_type": "open_id"},
                          json={"receive_id": open_id, "msg_type": "text",
                                "content": json.dumps({"text": text}, ensure_ascii=False)},
                          timeout=10, headers={"Authorization": f"Bearer {token}"})
        data = r.json()
        if data.get("code") == 0:
            return True, ""
        return False, f"code={data.get('code')}"
    except requests.RequestException as e:
        return False, repr(e)


def build_card(title: str, body: str, events: list, note: str = "") -> dict:
    """已迁 output/format.py（排版层，2026-10-18 行数纪律拆分）——保留转发，
    兼容既有调用与测试。"""
    return fmt.build_card(title, body, events, note)


def _load_cache() -> dict:
    try:
        return store.load_json("feishu_token.json", {})
    except OSError:
        return {}


def _save_cache(data: dict) -> None:
    try:
        store.save_json("feishu_token.json", data)  # 原子写（tmp+rename）
    except OSError:
        pass  # 缓存写失败不碍事：token 下次重取、open_id 由反馈事件再发现
