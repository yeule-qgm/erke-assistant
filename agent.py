"""L1 入口层：命令行入口——参数解析、配置加载、日志装配。巡检编排见 patrol.py。

用法：
  python agent.py                # 正常巡检（任务计划程序每小时调一次的就是它）
  python agent.py --dry-run      # 演练：打印将推送的内容，不真推、不记账、不写状态
  python agent.py --offline      # 调试：数据来自 offline_sample.json，不碰平台，
                                 # 状态读写走 state-offline/（与真实状态隔离，2026-09-28）
  python agent.py --init-llm-profile            # V3 一次性冷启动：全部偏好证据蒸馏
                                 # → 写偏好库（state/llm_profile.json）→ 终端打印过目
                                 # （--offline 同用 = 沙箱演练，真实档案不动）
  python agent.py --init-llm-profile --archive  # V3 第二步：过目确认后旧档案改名种子档
"循环"在进程外：定时器每小时唤起本文件，跑完即退（architecture-v1.md L0/L1 之分）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import store
from core import curate, scoring, watch
from patrol import run_once

BASE = Path(__file__).resolve().parent

DEFAULTS = {"score_line": 7, "quota_threshold": 10, "deadline_lead_hours": 24,
            "attend_lead_minutes": 60, "deliver_hours": [12, 23],
            "first_push_min_hour": 7, "removal_guard": True,
            "feedback_window_hours": 48,
            "daily_budget": 3, "history_keep_days": 90, "change_ttl_hours": 24}
RANGES = {"score_line": (1, 10), "quota_threshold": (1, 10**6),
          "deadline_lead_hours": (1, 24 * 30), "attend_lead_minutes": (1, 24 * 60),
          "first_push_min_hour": (0, 23), "feedback_window_hours": (1, 24 * 30),
          "change_ttl_hours": (1, 24 * 7),
          "daily_budget": (1, 99), "history_keep_days": (1, 3650)}


def main() -> int:
    args = _parse()
    log = _setup_logging()
    cfg = _load_config(log)
    if args.offline:
        store.set_state_dir(BASE / "state-offline")
        log.info("离线模式：状态读写走 state-offline/，真实 state/ 不受影响")
    if args.init_llm_profile:
        return _init_llm_profile(args, cfg, log)
    return run_once(args, cfg, log)


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="第二课堂活动助理·一次巡检")
    p.add_argument("--dry-run", action="store_true", help="演练：不推送、不记账、不写状态")
    p.add_argument("--offline", action="store_true", help="离线：用 offline_sample.json，不碰平台")
    p.add_argument("--init-llm-profile", action="store_true",
                   help="V3 一次性冷启动：蒸馏全部偏好证据 → 写偏好库并打印过目")
    p.add_argument("--archive", action="store_true",
                   help="（仅与 --init-llm-profile 同用）过目确认后旧档案改名种子档")
    p.add_argument("--force", action="store_true",
                   help="（仅与 --init-llm-profile 同用）偏好库已存在时仍覆盖重蒸馏")
    args = p.parse_args()
    if (args.archive or args.force) and not args.init_llm_profile:
        p.error("--archive/--force 仅与 --init-llm-profile 同用（先跑第一段并过目初稿）")
    return args


def _load_config(log: logging.Logger) -> dict:
    cfg = {"params": dict(DEFAULTS),
           "scoring": {"keyword_weight": 0.3},
           "llm": {"model": "glm-4-flash"},
           "curation": {"feedback_window_days": 30, "decision_window_days": 7,
                        "keyword_step": 3, "watch_step": 2, "snapshot_keep": 30},
           "platform": {"base_url": "https://young.ustc.edu.cn/login/wisdom-group-learning-bg/",
                        "children_endpoint": "item/scItem/selectSignChirdItem",
                        "max_children_per_series": 100},
           "mcp": {"cache_ttl_seconds": 300},
           "feishu": {"open_id": ""}}
    try:
        with open(BASE / "config.toml", "rb") as f:
            data = tomllib.load(f)
        cfg["params"].update(data.get("params", {}))
        cfg["scoring"].update(data.get("scoring", {}))
        cfg["llm"].update(data.get("llm", {}))
        cfg["curation"].update(data.get("curation", {}))
        cfg["platform"].update(data.get("platform", {}))
        cfg["mcp"].update(data.get("mcp", {}))
        cfg["feishu"].update(data.get("feishu", {}))
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning(f"读取 config.toml 失败，用默认参数：{e!r}")
    for key, (lo, hi) in RANGES.items():  # 参数写错时大声提醒，不静默失效
        v = cfg["params"].get(key)
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            log.warning(f"参数 {key}={v!r} 超出合理范围 [{lo}, {hi}]，请检查 config.toml")
    dv = cfg["params"].get("deliver_hours")
    if not (isinstance(dv, list) and dv
            and all(isinstance(h, int) and 0 <= h <= 23 for h in dv)):
        log.warning(f"参数 deliver_hours={dv!r} 应为 0~23 的整数列表（如 [12, 23]），请检查 config.toml")
    rg = cfg["params"].get("removal_guard")
    if not isinstance(rg, bool):
        log.warning(f"参数 removal_guard={rg!r} 应为 true/false，请检查 config.toml")
    mc = cfg["platform"].get("max_children_per_series")   # [platform] 子项目护栏（ADR-0015）
    if not isinstance(mc, int) or isinstance(mc, bool) or not 1 <= mc <= 500:
        log.warning(f"参数 [platform] max_children_per_series={mc!r} 超出合理范围 [1, 500]，请检查 config.toml")
    ttl = cfg["mcp"].get("cache_ttl_seconds")             # [mcp] 查询面节流（ADR-0017）
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 60 <= ttl <= 3600:
        log.warning(f"参数 [mcp] cache_ttl_seconds={ttl!r} 超出合理范围 [60, 3600]，已回缺省 300，请检查 config.toml")
        cfg["mcp"]["cache_ttl_seconds"] = 300
    kw = cfg["scoring"].get("keyword_weight")
    if not isinstance(kw, (int, float)) or not 0 <= kw <= 1:
        log.warning(f"参数 [scoring] keyword_weight={kw!r} 应为 0~1 的数，请检查 config.toml")
    for key, lo, hi in (("feedback_window_days", 1, 365),   # [curation] 调档窗口与幅度
                        ("decision_window_days", 1, 365),   # （spec 决策 12，03 票）
                        ("keyword_step", 1, 20), ("watch_step", 1, 20),
                        ("snapshot_keep", 1, 365)):
        v = cfg["curation"].get(key)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            log.warning(f"参数 [curation] {key}={v!r} 超出合理范围 [{lo}, {hi}]，请检查 config.toml")
    return cfg


# ------- V3 初始化命令（ADR-0013 初始化；spec 决策 11/16，01 票） -------

def _init_llm_profile(args, cfg: dict, log: logging.Logger) -> int:
    """两段式冷启动。第一段（无 --archive）：旧档案 + 主题词表 + signup 源全部
    标题 + 全部反馈流水 → 一次蒸馏 → 写偏好库 → 打印过目；偏好库已存在拒绝
    重跑（--force 显式覆盖）。任何失败：不写库、旧档不动（安全默认 = 不动）。"""
    md = store.preferences_md()
    if args.archive:
        return _archive_seed(md, md.with_name("preferences-seed.md"), log)
    if store.profile_exists() and not args.force:
        log.error("偏好库已存在——重跑会覆盖已学内容，已拒绝；确认覆盖请加 --force")
        return 1
    if not md.exists() and store.prepare_fixture(BASE / "preferences.md"):
        log.info("离线演练：已把真实偏好档案复制为沙箱夹具（真实档案不动）")
    prefs_text = store.read_text(md)
    body = "\n".join(ln for ln in prefs_text.splitlines()
                     if not ln.strip().startswith(("#", ">")))  # 同打分的激活判断口径
    if not body.strip() or "待填" in body:
        log.error(f"偏好档案还没填写或不存在（{md}）——骨架期不初始化")
        return 1
    # 证据装配：signup 源全部标题（带开始时刻；未开始才可作为关注候选，决策 18
    # 预筛从严——快照缺失/无开始时刻按"未知"处理，不可点名）
    themes = store.load_themes(BASE / "themes.txt")
    watch_state = store.load_json("watch.json", {})
    snapshots = store.load_json("activities.json", {})
    now = datetime.now()
    signups, candidates = [], {}
    for aid, e in watch_state.items():
        if "signup" not in (e.get("sources") or []):
            continue
        act = (snapshots.get(aid) or {}).get("activity") or {}
        start = store.parse_dt(act.get("start_time"))
        if start and start > now:
            state_label, eligible = "未开始", True
        elif start:
            state_label, eligible = "已开始", False
        else:
            state_label, eligible = "未知", False
        signups.append({"activity_id": aid, "title": e.get("title", ""),
                        "start_time": act.get("start_time", ""), "状态": state_label})
        if eligible:
            candidates[aid] = e.get("title", "")
    feedback = [{k: e.get(k) for k in ("seq", "time", "title", "action")}
                for e in store.load_json("feedback.json", [])]  # 不带 from：个人标识不进 prompt
    log.info(f"证据装配：档案 {len(prefs_text)} 字 · 主题 {len(themes)} 词 · "
             f"报名 {len(signups)} 条（可点名 {len(candidates)}）· 反馈 {len(feedback)} 条")
    distilled, why = curate.distill(
        {"prefs_text": prefs_text, "themes": themes, "signups": signups,
         "feedback": feedback}, cfg["llm"])
    if distilled is None:
        log.error(f"初始化中止，未写偏好库、旧档不动：{why}")
        return 1
    now_iso = now.isoformat(timespec="minutes")
    profile, picks = curate.build_initial_profile(
        distilled, prefs_text, scoring.extract_keywords(prefs_text),
        scoring.extract_excludes(prefs_text), now_iso, candidates)
    try:
        store.save_profile(profile)
    except ValueError as e:
        log.error(f"初始化中止（{e}）——偏好库未写、旧档不动")
        return 1
    for line in watch.add_llm(watch_state, picks, now_iso):
        log.info(line)
    if picks:
        store.save_json("watch.json", watch_state)
    print(f"\n【偏好库初稿】已写入 {store.STATE / store.PROFILE}"
          "（系统按『偏好库存在』切换 V3 读取；删除该文件即整体回退，决策 16）\n")
    print(curate.render_review(profile, picks))
    print(f"\n过目没问题就归档旧档：python agent.py --init-llm-profile --archive\n")
    return 0


def _archive_seed(md: Path, seed_md: Path, log: logging.Logger) -> int:
    """第二段（--archive）：偏好库存在且可用才把旧档案改名种子档——显式确认
    后才执行，归档后旧档案不再被任何代码消费（种子文本已照抄进偏好库）。"""
    if not store.profile_exists():
        log.error("偏好库还不存在——先跑第一段（--init-llm-profile 不带 --archive）并过目")
        return 1
    if store.load_profile() is None:
        log.error("偏好库文件存在但损坏/不合法——暂不归档旧档（它还是回退链路），"
                  "先 --init-llm-profile --force 重蒸馏")
        return 1
    if not md.exists():
        if seed_md.exists():
            log.info(f"旧档案已归档过（{seed_md.name} 在）——无事可做")
            return 0
        log.error(f"旧偏好档案不存在（{md}）——没东西可归档")
        return 1
    md.rename(seed_md)
    log.info(f"归档完成：{md.name} → {seed_md.name}（种子档只读，种子全文已照抄进偏好库）")
    return 0


def _setup_logging() -> logging.Logger:
    (BASE / "logs").mkdir(exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # openai 客户端每个请求刷一行，压掉
    handler = TimedRotatingFileHandler(BASE / "logs" / "agent.log",
                                       when="W0", interval=1,  # 每周一轮转，保留 4 周
                                       backupCount=4, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stdout)])
    return logging.getLogger("agent")


if __name__ == "__main__":
    sys.exit(main())
