"""L3 数据源层：平台 API 客户端（允许黑盒——关键 20% 之外）。

职责：会话凭据复用（ADR-0001）、抓活动列表、读"我的报名"、拉系列子项目清单（ADR-0015）。
与平台说话的代码只存在于本文件；core/ 与 output/ 不 import 本模块（ADR-0005⑤）。

凭据：环境变量 ERKE_PLATFORM_TOKEN = 浏览器登录后复制的 X-Access-Token（README"换票三步"）。
失效时抛 TokenExpired，上层推"该换票了"告警后安静退出。

真实端点来自平台前端 JS 里的明文端点名（凡标 [待实测] 处需首次真跑核对），
请求参数与响应字段是 Jeecg 惯例推断。

内部统一的活动格式（下游全部依赖这个形状，不关心来源）：
  {"id": str, "title": str, "desc": str,
   "start_time": "YYYY-MM-DD HH:MM" | None,   # 活动开始
   "deadline":  "YYYY-MM-DD HH:MM" | None,    # 报名截止
   "location": str, "status": str,
   "quota_total": int | None, "quota_left": int | None,
   "pid": str,       # 母活动 id——系列子项目非空，顶层为空（平台 -1 归一，ADR-0015）
   "category": str}  # 平台条目类目："1"=系列项目（ADR-0015；其余为 "0"/空）

--offline 模式：不碰网络，读 offline_sample.json（调试/演示；时间支持 "+1.5h" 相对写法）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
TIMEOUT = 20


class TokenExpired(Exception):
    """平台凭据失效——上层应推"该换票了"（不占预算）后正常退出。"""


class FetchError(Exception):
    """网络/接口异常——上层记日志退出，下次巡检自然重试（Q23 跨轮重试）。"""


def fetch(offline: bool, config: dict, wall_clock_seconds: int = 120,
          watched_ids: set | None = None,
          series_ids: set | None = None) -> tuple[list[dict], list[str], list[dict], list[str]]:
    """返回 (活动列表, 我的报名 id 列表, 系列子项目, 子项目日志行)。

    wall_clock_seconds 是整次抓取的总时长保险丝：requests 的 timeout 只管"两次字节之间"，
    服务端滴漏式响应能把单次请求无限挂起——无人值守场景一次挂起=后续巡检全部停摆
    （任务计划程序默认不叠加实例）。2026-09-21 23:00 实测踩中，故有此保险丝。
    列表/报名/子项目共用同一条（ADR-0015 护栏：chird 每轮每系列 1 个 GET 纳入总保险丝）。

    watched_ids = 当前关注清单 id 集：新列入"报名中"的系列母活动（category=1 且被
    关注）当场拉子项目；series_ids = 账本已知且仍被关注的系列母活动——下架期
    （用户实测母活动会"时不时下架/上架"）也不停轮询（ADR-0015 决定 2 覆盖
    "关注中的全体系列母活动"）。两者皆空 = 不拉子项目。
    """
    if offline:
        return _fetch_offline(config, watched_ids, series_ids)
    return _run_fused(wall_clock_seconds,
                      lambda: _fetch_real(config, watched_ids, series_ids))


def _run_fused(seconds: int, fn):
    """总时长保险丝的线程实现：fn 在 seconds 内未完成即放弃（FetchError）。"""
    import threading
    outcome: dict = {}

    def _work():
        try:
            outcome["value"] = fn()
        except Exception as e:  # noqa: BLE001 - 统一转交主线程
            outcome["error"] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise FetchError(f"抓取超过 {seconds}s 未完成，强制放弃（服务端疑似卡死）")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


# ---------------- 离线模式（调试/演示） ----------------

def _fetch_offline(config: dict, watched_ids: set | None = None,
                   series_ids: set | None = None) -> tuple[list[dict], list[str], list[dict], list[str]]:
    data = json.loads((BASE_DIR / "tests" / "offline_sample.json").read_text(encoding="utf-8"))
    anchor = _dt_or_none(data.get("_anchor")) or datetime.now()
    acts = [_relative_times(dict(a), anchor) for a in data["activities"]]
    regs = [str(i) for i in data.get("registrations", [])]
    kids, notes = _children_of(acts, watched_ids, series_ids,
                                lambda p: _children_offline(p, _limit(config)))
    return acts, regs, kids, notes


def _relative_times(a: dict, anchor: datetime) -> dict:
    """把 "+1.5h" 相对锚点换算成绝对时间。锚点固定（写在 JSON 的 _anchor 里），
    这样每次运行算出的绝对时间相同——否则快照比对会把漂移误判成"时间变更"。"""
    for k in ("start_time", "deadline"):
        v = a.get(k)
        if isinstance(v, str) and v.startswith("+") and v.endswith("h"):
            try:
                a[k] = (anchor + timedelta(hours=float(v[1:-1]))).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
    return a


def _dt_or_none(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


# ---------------- 真实平台 ----------------

def _fetch_real(config: dict, watched_ids: set | None = None,
                series_ids: set | None = None) -> tuple[list[dict], list[str], list[dict], list[str]]:
    token = os.environ.get("ERKE_PLATFORM_TOKEN", "")
    if not token:
        raise FetchError("未设置 ERKE_PLATFORM_TOKEN（按 README 换票三步获取）")
    plat = config.get("platform", {})
    base = plat.get("base_url", "https://young.ustc.edu.cn/login/wisdom-group-learning-bg/")
    headers = {"X-Access-Token": token}
    # "报名中"活动列表（平台级，本来就是全校可报名的活动）——翻页抓全量
    raw_acts = _get_pages(base + plat.get("list_endpoint", "mobile/item/enrolmentList"),
                          headers)
    # 我的报名：HaveSign 页同款接口，服务端按登录态（token）过滤，无需任何用户参数。
    # 单页 100 条封顶。注意别用 item/scItemRegistration/list——那是不带过滤的全平台名单。
    raw_regs = _get_pages(base + plat.get("registration_endpoint",
                                          "item/scParticipateItem/list"),
                          headers, extra={"queryCatelog": 0}, max_pages=1, page_size=100)
    acts = [_map_activity(r) for r in raw_acts]
    url = base + plat.get("children_endpoint", "item/scItem/selectSignChirdItem")
    kids, notes = _children_of(
        acts, watched_ids, series_ids,
        lambda p: _children_real(p, url, headers, _limit(config)))
    return (acts,
            [str(_pick(r, "itemId", "id")) for r in raw_regs],  # 实测：id 即活动 id
            kids, notes)


def _children_of(acts: list[dict], watched_ids, series_ids, fetch) -> tuple[list[dict], list[str]]:
    """系列母活动选取（ADR-0015 决定 2）：当轮在架的（category=1 且被关注）
    ∪ 账本已知仍被关注的（下架期不停轮询）。无母活动时不发起拉取。"""
    parents = set(series_ids or ())
    parents |= {a["id"] for a in acts
                if str(a.get("category") or "") == "1" and a["id"] in (watched_ids or ())}
    return fetch(sorted(parents)) if parents else ([], [])


# 单接口数据范围护栏：正常情况下活动列表 65 条、我的报名几十条；
# total 远超此值 = 接口没按用户过滤，全量抓取等于爬全校数据（安全红线）
MAX_REASONABLE_TOTAL = 5000


def _get_pages(url: str, headers: dict, extra: dict | None = None,
               max_pages: int | None = None, page_size: int = 50) -> list[dict]:
    """翻页抓全量。已实测（2026-09-21）：本平台分页接口吃 GET+查询参数（POST 报权限错），
    响应包 {success, result:{records,total}}；success:false 必须当错误抛出——
    当空列表静默放过会漏推（首跑实测教训）。"""
    out, page = [], 1
    while True:
        params = {**(extra or {}), "pageNo": page, "pageSize": page_size}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise FetchError(f"网络错误：{e!r}") from e
        body = _safe_json(r)
        msg = str(body.get("message", ""))
        if "token" in msg.lower():
            raise TokenExpired(msg[:120])
        if not body.get("success"):
            raise FetchError(f"接口拒绝：{msg[:120] or r.text[:120]}")
        result = body.get("result")
        records = (result.get("records") or []) if isinstance(result, dict) else []
        out.extend(x for x in records if isinstance(x, dict))
        total = int(result.get("total") or 0) if isinstance(result, dict) else len(records)
        if total > MAX_REASONABLE_TOTAL:
            raise FetchError(f"{url.split('/')[-1]} 返回 total={total}——"
                             f"疑似未按用户过滤（安全护栏），拒绝全量抓取")
        if not records or page * page_size >= total or (max_pages and page >= max_pages):
            return out
        page += 1


def refresh_quota(activity_ids: list[str], config: dict) -> dict[str, dict]:
    """对关注中的活动逐个查详情，取实时名额。

    为什么存在：列表接口的 applyNum 恒为 0（2026-09-22 实测 66/66 全零，静态假数），
    名额真数据只在详情接口（同活动 列表"已报0/150" vs 详情"已报29/150"）。
    返回 {活动id: {"quota_total": 总数, "quota_left": 剩余}}；单个查询失败跳过
    （本次无更新，下次巡检再来）；token 失效向上抛。
    """
    token = os.environ.get("ERKE_PLATFORM_TOKEN", "")
    if not token:
        raise TokenExpired("未设置 ERKE_PLATFORM_TOKEN")
    base = config.get("platform", {}).get(
        "base_url", "https://young.ustc.edu.cn/login/wisdom-group-learning-bg/")
    out: dict[str, dict] = {}
    for aid in activity_ids:
        try:
            r = requests.get(base + "item/scItem/queryById", params={"id": aid},
                             headers={"X-Access-Token": token}, timeout=TIMEOUT)
        except requests.RequestException:
            continue  # 单个失败不拖垮整轮
        body = _safe_json(r)
        msg = str(body.get("message", ""))
        if "token" in msg.lower():
            raise TokenExpired(msg[:120])
        det = body.get("result") if body.get("success") else None
        if not isinstance(det, dict):
            continue
        total, applied = _int_or_none(det.get("peopleNum")), _int_or_none(det.get("applyNum"))
        if total is None:
            continue
        out[aid] = {"quota_total": total,
                    "quota_left": total - applied if applied is not None else None}
    return out


def fetch_children(parent_ids: list[str], config: dict,
                   offline: bool = False) -> tuple[list[dict], list[str]]:
    """系列母活动的子项目清单（ADR-0015）。返回 (子项目列表, 日志行)。

    巡检主链路不直接调本函数——fetch() 在总保险丝线程内拉子项目（watched_ids/
    series_ids）；本函数供单独取数与 offline 夹具测试使用。2026-11-03 实测：
    result 直接是列表（无分页信封）；"Chird"是平台原始拼写错误。
    """
    if offline:
        return _children_offline(parent_ids, _limit(config))
    token = os.environ.get("ERKE_PLATFORM_TOKEN", "")
    if not token:
        raise TokenExpired("未设置 ERKE_PLATFORM_TOKEN")
    plat = config.get("platform", {})
    base = plat.get("base_url", "https://young.ustc.edu.cn/login/wisdom-group-learning-bg/")
    url = base + plat.get("children_endpoint", "item/scItem/selectSignChirdItem")
    return _children_real(parent_ids, url, {"X-Access-Token": token}, _limit(config))


def fetch_enrolment(config: dict, offline: bool = False) -> list[dict]:
    """『报名中』活动列表的独立取数（ADR-0017，MCP 查询面用）。返回内部格式列表。

    巡检主链路不走本函数——fetch() 把列表/报名/子项目并入同一条总保险丝；
    本函数供单独取数与 offline 夹具测试使用（fetch_children/refresh_quota 同款）。
    护栏与巡检同源：翻页全量、success:false 当错误抛、total>5000 拒抓、
    60s 保险丝（本端点正常 2~3 页，留出翻页余量）。"""
    if offline:
        acts, _, _, _ = _fetch_offline(config)
        return acts
    token = os.environ.get("ERKE_PLATFORM_TOKEN", "")
    if not token:
        raise FetchError("未设置 ERKE_PLATFORM_TOKEN（按 README『换票三步』获取）")
    plat = config.get("platform", {})
    base = plat.get("base_url", "https://young.ustc.edu.cn/login/wisdom-group-learning-bg/")
    headers = {"X-Access-Token": token}
    raw = _run_fused(60, lambda: _get_pages(
        base + plat.get("list_endpoint", "mobile/item/enrolmentList"), headers))
    return [_map_activity(r) for r in raw]


def _limit(config: dict) -> int:
    """max_children_per_series 的容错读取：坏值回退缺省 100，下限 1（0=全拒收没意义）。"""
    try:
        v = int(config.get("platform", {}).get("max_children_per_series", 100) or 100)
    except (TypeError, ValueError):
        v = 100
    return max(1, v)


def _children_real(parent_ids: list[str], url: str, headers: dict,
                   limit: int) -> tuple[list[dict], list[str]]:
    """每轮每母活动 1 个 GET；单个母活动失败跳过本轮（refresh_quota 同款容错，
    下轮自然重试）；返回数 > limit 拒收该系列并告警（脏数据护栏，与
    MAX_REASONABLE_TOTAL 同族）。token 失效向上抛。"""
    out: list[dict] = []
    notes: list[str] = []
    for pid in parent_ids:
        try:
            r = requests.get(url, params={"id": str(pid)}, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            notes.append(f"[子项目] 『{pid}』拉取失败，跳过本轮：{e!r}")
            continue
        body = _safe_json(r)
        msg = str(body.get("message", ""))
        if "token" in msg.lower():
            raise TokenExpired(msg[:120])
        if not body.get("success"):
            notes.append(f"[子项目] 『{pid}』接口拒绝，跳过本轮：{msg[:80]}")
            continue
        rows = _result_rows(body.get("result"))
        if len(rows) > limit:
            notes.append(f"[警告] 系列『{pid}』返回 {len(rows)} 个子项目（>{limit}），"
                         f"疑似脏数据，拒收该系列")
            continue
        out.extend(_child_rows(rows, str(pid)))
    return out, notes


def _children_offline(parent_ids: list[str], limit: int) -> tuple[list[dict], list[str]]:
    """offline 演练：子项目读 offline_sample.json 的 _children 夹具（键=母活动 id，
    行=内部格式——与活动条目同款，不走 _map_activity；相对时间同款锚点语义，
    场次类条目常用绝对时间更直观）。"""
    data = json.loads((BASE_DIR / "tests" / "offline_sample.json").read_text(encoding="utf-8"))
    anchor = _dt_or_none(data.get("_anchor")) or datetime.now()
    fixture = data.get("_children") or {}
    out: list[dict] = []
    notes: list[str] = []
    for pid in parent_ids:
        rows = [r for r in (fixture.get(pid) or []) if isinstance(r, dict)]
        if len(rows) > limit:
            notes.append(f"[警告] 系列『{pid}』返回 {len(rows)} 个子项目（>{limit}），"
                         f"疑似脏数据，拒收该系列")
            continue
        for r in rows:
            c = _relative_times(dict(r), anchor)
            c["pid"] = str(pid)  # 以请求的母活动 id 为准（与 online 分支同规则）
            out.append(c)
    return out, notes


def _child_rows(rows: list[dict], pid: str) -> list[dict]:
    """平台行 → 内部格式子项目；pid 以请求的母活动 id 为准（权威来源，不信行内回填）。"""
    out = []
    for r in rows:
        c = _map_activity(r)
        c["pid"] = str(pid)
        out.append(c)
    return out


def _result_rows(result) -> list[dict]:
    """子项目接口 result 的容错提取（实测为列表；留 records 分支防平台改版）。"""
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        return [x for x in (result.get("records") or []) if isinstance(x, dict)]
    return []


def _map_activity(raw: dict) -> dict:
    """平台原始字段 → 内部格式。字段名 2026-09-21 已按真实响应核对。"""
    quota_total = _int_or_none(_pick(raw, "peopleNum", "totalNumber", "quota", "capacity"))
    apply_num = _int_or_none(_pick(raw, "applyNum"))
    quota_left = quota_total - apply_num if (quota_total is not None
                                             and apply_num is not None) else None
    place = raw.get("placeInfo") or ""
    if not place and isinstance(raw.get("itemPlaceDTO"), dict):
        place = str(raw["itemPlaceDTO"].get("name")
                    or raw["itemPlaceDTO"].get("address") or "")
    pid = str(raw.get("pid") or "")
    if pid == "-1":
        pid = ""  # 平台以 -1 表示无母活动（2026-11-03 实测 enrolmentList 顶层行）
    return {
        "id": str(raw.get("id") or raw.get("itemId") or ""),
        "title": str(raw.get("itemName") or raw.get("title") or ""),
        "desc": _clean_text(str(raw.get("baseContent")
                                or raw.get("itemIntroduce") or "")),
        "start_time": _pick(raw, "st", "startTime", default=None),          # 活动开始
        "deadline": _pick(raw, "applyEt", "registrationDeadline", default=None),  # 报名截止
        "location": str(place),
        "status": str(raw.get("itemStatus_dictText") or raw.get("itemStatus")
                      or raw.get("status") or ""),
        "quota_total": quota_total,
        "quota_left": quota_left,
        "pid": pid,
        "category": str(raw.get("itemCategory") or ""),
    }


def _clean_text(html: str) -> str:
    """简介字段（baseContent）是 HTML：去标签、压空白，否则主题匹配和 LLM 都会被 <br/> 糊脸。"""
    text = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pick(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _safe_json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}
