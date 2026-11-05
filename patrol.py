"""L1 入口层：一次巡检的编排（关键 20%·主循环骨架）。

2026-09-22 自 agent.py 拆出（状态读写/互斥锁在 store.py）。编排放顶层而非 core/ 包内
——编排要装配数据源与输出层，core/ 保持零运输依赖（ADR-0005⑤）。

巡检顺序：回滚请求检测（V3，偏好库在位时的『撤销调档』）→ 抓取（含关注系列
的子项目，ADR-0015）→ 调档（V3，偏好库在位、每日一次且水位前进才上岗）→
主题命中（抢救覆盖全部活动）→ chird 通道（系列新场次）→ LLM 打分 → 动向 →
预算仲裁 → 推送（调档生效当轮尾部附"今日档案调整"行）→ 快照落盘。
事件永不丢：推送失败或排版装不下都回待发队列。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from core import budget, curate, develop, feedback, recommend, scoring, series, watch
from output import format as fmt
from output import push
from source import platform
from store import (acquire_lock, cleanup, decide, load_json, load_profile,
                   load_themes, parse_dt, process_rollback, read_text,
                   release_lock, rollback_requested, save_all, save_json,
                   save_mentions, save_profile, snapshot_curation)

BASE = Path(__file__).resolve().parent


def run_once(args, cfg: dict, log: logging.Logger) -> int:
    """巡检入口。真实巡检带互斥锁：手动巡检与定时巡检撞车时，后到者放弃
    （2026-09-22 实测：两次巡检交叠，后结束者用旧状态整体覆盖前者，导致重复推送）。"""
    if args.dry_run:
        return _patrol(args, cfg, log)  # 演练不写状态，无需互斥
    if not acquire_lock():
        return 0
    try:
        return _patrol(args, cfg, log)
    finally:
        release_lock()


def _patrol(args, cfg: dict, log: logging.Logger) -> int:
    params = cfg["params"]
    now = datetime.now()
    now_iso = now.isoformat(timespec="minutes")
    tag = ("[演练]" if args.dry_run else "") + ("[离线]" if args.offline else "")
    log.info(f"=== 巡检开始 {tag} ===")

    # ⓪ 回滚请求（V3 05 票，ADR-0013 决定 10）：『撤销调档』口令由反馈服务登记
    # 请求文件（06 票），巡检首步消费——恢复最近调档快照（偏好库+关注清单）并
    # 删请求。必须先于一切状态装配（恢复的就是本轮要读的档案）；写权限保持
    # 单写者（反馈服务不碰偏好库）。偏好库不在场（决策 16 回退态）链内作废请求。
    # 演练只报告不执行（演练不写状态）。
    if rollback_requested():
        if args.dry_run:
            log.info("[回滚] 检测到『撤销调档』请求——演练不执行恢复，真实巡检下轮生效")
        else:
            restored, line = process_rollback(now)
            (log.info if restored else log.error)(f"[回滚] {line}")

    # ① 抓取（含系列子项目，ADR-0015）：token 失效 → 告警换票；其他失败 → 计数，
    # 连续 3 次推系统告警（不占预算）。列表/报名/子项目共用同一条 120s 总保险丝；
    # 母活动选取 = 当轮在架的（category=1 且被关注）∪ 账本已知仍被关注的（下架期不停轮询）
    reports = load_json("reports.json", {"date": "", "sent": 0, "events": {}})
    snapshots = load_json("activities.json", {})
    watch_state = load_json("watch.json", {})
    series_known = {aid for aid, s in snapshots.items()
                    if str((s.get("activity") or {}).get("category") or "") == "1"
                    and aid in watch_state}
    try:
        activities, registrations, children, child_notes = platform.fetch(
            args.offline, cfg, watched_ids=set(watch_state), series_ids=series_known)
    except platform.TokenExpired:
        push.send_markdown("【该换票了】",
                           "第二课堂助手连不上平台了。请按 README『换票三步』更新 ERKE_PLATFORM_TOKEN，"
                           "下次巡检自动恢复。（系统告警，不占每日预算）")
        log.warning("平台凭据失效，已推送换票告警，本次巡检结束")
        return 0
    except platform.FetchError as e:
        reports["fetch_fails"] = reports.get("fetch_fails", 0) + 1
        log.error(f"抓取失败（连续第 {reports['fetch_fails']} 次），本次巡检放弃：{e}")
        if reports["fetch_fails"] >= 3 and not reports.get("fetch_alerted"):
            push.send_markdown("【系统告警】",
                               f"第二课堂助手已连续 {reports['fetch_fails']} 次抓取失败，"
                               "请查看 logs/agent.log 排查（token 失效按 README 换票三步）。（不占每日预算）")
            reports["fetch_alerted"] = True
        if not args.dry_run: save_json("reports.json", reports)
        return 1
    reports["fetch_fails"] = 0
    reports["fetch_alerted"] = False
    log.info(f"抓到 {len(activities)} 个活动，{len(registrations)} 个我的报名")

    # ② 装配账本与偏好原料（打分/调档共用；换料在 ②.6——调档成功后才能用新料）。
    # feedback.json 从这里加载、⑥.1 复用（04 票口径不变）
    queue = load_json("queue.json", [])
    reported = set(reports.get("events", {}))
    feedback_log = load_json("feedback.json", [])
    profile = load_profile()
    themes = load_themes(BASE / "themes.txt")

    # ①.5 系列子项目并入活动列表（拉取在 ① 的同一道保险丝内完成）。子项目只走
    # chird 通道：③ 主题匹配与 ⑥ 打分池均按 pid 排除（发现通道唯一性）。
    for note in child_notes:
        if note.startswith("[警告]"):
            log.warning(note)   # 拒收告警升级：出问题先看日志（AGENTS 易用性要求）
        else:
            log.info(note)
    if children:
        activities = activities + children
        log.info(f"[子项目] 并入系列子项目 {len(children)} 个")
    titles = {a["id"]: a["title"] for a in activities}
    by_id = {a["id"]: a for a in activities}

    # ②.5 调档（V3 05 票，ADR-0013 spec 决策 4/16）：偏好库在位才走——报名增删
    # 流水维护（持久累计，len 即水位计数器）→ 03 票调档链（每日一次、水位前进
    # 才上岗，链内自查）；应用成功当轮即换打分链的料（②.6）并记汇总尾行。
    # 偏好库缺席整体跳过、流水不维护（回退态，决策 16）——初始化后首巡会把
    # 全部现存报名计为新证据，首次调档由最早一次巡检完成。候选面 = 顶层活动：
    # 系列子项目走 chird 通道（ADR-0015 发现通道唯一性），curator 点名已开始
    # 活动零动向价值（决策 18 时限闸）。
    curation_note = ""
    if profile is not None:
        feed = load_json("signup_feed.json", {"last": {}, "entries": []})
        fed, feed_logs = curate.update_signup_feed(feed, registrations, titles, now_iso)
        for line in feed_logs:
            log.info(f"[调档] {line}")
        changeset, _why = run_curation(
            profile, feedback_log, fed["entries"], snapshots,
            [a for a in activities if not a.get("pid")], watch_state,
            cfg, now, log, dry_run=args.dry_run)
        if not args.dry_run and fed != feed:
            save_json("signup_feed.json", fed)
        if changeset is not None:
            profile = changeset["profile"]
            curation_note = changeset.get("note") or ""  # 尾行由调档链算好（零变更=空）

    # ②.6 打分链装配（V3 04 票：偏好库存在 → 偏好段渲染 + 生效词表 = 种子 ∪
    # 活数组 + 反馈流水近 30 天独立成节；不存在 → 回退旧偏好档案，与换料前逐
    # 字节一致，spec 决策 16。调档成功时 profile 已是新版——本轮打分即用新料）
    prefs_text, keywords, excludes = scoring_inputs(profile, feedback_log, cfg, now)
    if profile:
        log.info(f"[偏好] 打分读偏好库：生效关键词 {len(keywords)}、排除词 "
                 f"{len(excludes)}（种子 ∪ 活数组），流水 {len(feedback_log)} 条")
    else:
        log.info("[偏好] 偏好库不存在，打分读旧偏好档案（回退，spec 决策 16）")

    # ③ 主题命中计算（关注清单的统一更新在 ⑥.1——打分之后，才能三源一次传入）
    # 排除词命中即一律不推：压倒主题必推、不进打分池、不进关注（ADR-0011）；
    # 子项目（pid 非空）不做主题匹配——正当性继承母活动的关注，防止片名撞词双推（ADR-0015）
    excl = {a["id"] for a in activities if recommend.excluded(a, excludes)}
    theme_hit_ids = [a["id"] for a in activities if not a.get("pid") and a["id"] not in excl
                     and recommend.theme_matches(a, themes)]

    # ③.5 黑名单（ADR-0008）：主题命中/已报名自动解除（读侧计算，blocklist 归反馈服务单写）
    blocked = (feedback.blocked(load_json("blocklist.json", {}))
               - set(theme_hit_ids) - set(registrations))

    # ④ 首次运行：静默建库（否则全站皆"新"，炸预算）。子项目不进建库基线——
    # 下一轮经 chird 通道入账，"报名中且未开始"的存量场次由此破例推一次（ADR-0015）
    if not snapshots:
        for a in activities:
            if a.get("pid"):
                continue
            decide(snapshots, a, {"kind": "bootstrap", "at": now_iso}, now_iso)
        log.info(f"首次运行：{len(snapshots)} 个活动入库建基线，静默不推送")
        if not args.dry_run:
            save_all(snapshots, watch_state, queue, reports)
        return 0

    events: list[dict] = []
    rec_watch_ids: list[str] = []  # 打分达标的活动 → 进关注（名额空缺等动向同样生效）
    registry = reports.setdefault("events", {})

    def _fire(e: dict) -> None:
        """事件入列并立刻登记 once-only 键——登记在创建时而非发送时，
        否则时点外未发送的事件会在下轮重复生成（回归实测踩中）。"""
        events.append(e)
        if e.get("key"):
            registry[e["key"]] = now_iso

    # ⑤ 主题命中——抢救式覆盖全部活动（Q25 加主题即补推；已推过的不重复；排除词已滤除）
    def _actionable(a: dict) -> bool:  # 已开始或报名截止已过的不再推（与 chird 通道同一判定）
        return series.actionable(a, now)

    for a in (by_id[i] for i in theme_hit_ids):
        kind = ((snapshots.get(a["id"]) or {}).get("decision") or {}).get("kind")
        if (kind in ("theme", "rec") or f"{a['id']}:theme" in reported
                or not _actionable(a)):
            continue
        _fire(develop.make_event(a, "theme", "新活动", reason="主题命中（必推）",
                                 key=f"{a['id']}:theme", now_iso=now_iso,
                                 valid_until=a.get("deadline") or a.get("start_time")))
        decide(snapshots, a, {"kind": "theme", "at": now_iso}, now_iso)
        log.info(f"[推荐] 必推『{a['title']}』（主题命中）")

    # ⑤.5 chird 通道（ADR-0015）：关注系列的新子项目 = 账本无此 id。新且可行动
    # （未开始且报名未截止）且母活动未被拉黑/排除 → 新场次事件 + watch 加 chird 源；
    # 其余（历史场次、母活动被滤）只入账 kind=chird——首拉基线轮不刷屏，"报名中且
    # 未开始"的存量场次破例推一次由此天然成立。
    chird_watch_ids: list[str] = []
    for c in series.new_children(children, snapshots):
        pid = c.get("pid") or ""
        if series.actionable(c, now) and pid not in blocked and pid not in excl:
            _fire(series.make_event(c, titles.get(pid) or pid, now_iso))
            chird_watch_ids.append(c["id"])
            log.info(f"[新场次] 『{c['title']}』（系列『{titles.get(pid) or pid}』新子项目，可行动）")
        decide(snapshots, c, {"kind": "chird", "at": now_iso}, now_iso)
    for line in watch.add_chird(watch_state, chird_watch_ids, now_iso, titles):
        log.info(f"[新场次] {line}")

    # ⑥ LLM 打分池：新活动 + 待评分（跨轮重试）+ 建库积压。不可行动（已开始/已截止）
    # 不打分——记"expired"防逐小时重试白花 LLM（2026-10-01《风格练习》实测）；
    # 子项目不进打分池——片名打分无语义，正当性继承母活动的关注（ADR-0015）
    pool_ids = {aid for aid in by_id if not by_id[aid].get("pid")
                and (aid not in snapshots or snapshots[aid].get("decision") is None)}
    for aid in [aid for aid in pool_ids if not _actionable(by_id[aid])]:
        decide(snapshots, by_id[aid], {"kind": "expired", "at": now_iso}, now_iso)
        log.info(f"[推荐] 跳过不可行动活动『{by_id[aid]['title']}』（已开始或截止已过）")
    pool_ids = {aid for aid in pool_ids if _actionable(by_id[aid])}
    pool_ids -= {e["activity_id"] for e in events}  # 已被主题命中的不再进打分池
    pool_ids -= blocked  # 黑名单不进打分池（用户说过不感兴趣；主题命中走 ⑤ 解除）
    pool_ids -= excl  # 排除词不进打分池（ADR-0011）——不花 LLM token 且永不推荐
    backlog = [s["activity"] for aid, s in snapshots.items()
               if (s.get("decision") or {}).get("kind") == "bootstrap"
               and aid in by_id and not by_id[aid].get("pid")
               and _actionable(by_id[aid])
               and aid not in blocked and aid not in excl]
    pool: list[dict] = []
    pooled: set[str] = set()
    for a in [by_id[i] for i in pool_ids if i in by_id] + backlog:
        if a["id"] not in pooled:
            pooled.add(a["id"])
            pool.append(a)
    if pool:
        scores, why = recommend.llm_score(pool, prefs_text, cfg["llm"], themes)
        if why:
            log.info(f"[推荐] {why}")
        scores = scoring.enrich(scores, pool, keywords, cfg["scoring"]["keyword_weight"])  # V2b 合成
        for a in pool:
            s = scores.get(a["id"])
            if s is None:
                continue  # 保持原状（待评分/建库态），下次巡检重试
            if s["score"] >= params["score_line"]:
                _fire(develop.make_event(
                    a, "recommend", f"综合 {s['score']}/10（LLM {s['llm']}＋关键词 {s['keyword']}）",
                    reason=s["reason"], now_iso=now_iso,
                    valid_until=a.get("deadline") or a.get("start_time")))
                decide(snapshots, a, {"kind": "rec", "llm": s["llm"], "keyword": s["keyword"],
                                      "score": s["score"], "at": now_iso}, now_iso)
                rec_watch_ids.append(a["id"])
                log.info(f"[推荐] 推『{a['title']}』（综合 {s['score']}＝LLM {s['llm']}＋关键词 {s['keyword']}：{s['reason']}）")
            else:
                decide(snapshots, a, {"kind": "filtered", "llm": s["llm"], "keyword": s["keyword"],
                                      "score": s["score"], "at": now_iso}, now_iso)
                log.info(f"[推荐] 过滤『{a['title']}』（综合 {s['score']} 分，低于 {params['score_line']}）")
        if len(pool) > len(scores):
            log.info(f"[推荐] 待评分 {len(pool) - len(scores)} 个，下次巡检自动重试")

    # ⑥.1 关注统一更新：各源一次传入（2026-09-22；interest 消费反馈流水，游标存 reports）
    interest_ids, seq = feedback.consume(feedback_log,
                                         reports.get("feedback_seq", 0))
    for line in watch.update(watch_state, theme_hit_ids, registrations,
                             now_iso, titles, rec_watch_ids, interest_ids,
                             removal_guard=params.get("removal_guard", True)):
        log.info(line)
    reports["feedback_seq"] = seq

    # ⑥.2 黑名单/排除词活动不产生任何动向（可推名单 = 关注 - 黑名单 - 排除词；
    # 主题命中已在上一步解除黑名单。排除词的历史关注条目残留但不生效——ADR-0011
    # "排除词压倒一切"对动向同样成立，如"高新校区"）。ADR-0015：母活动被拉黑/排除
    # 的子项目同滤（读侧过滤；用户对场次点"不感兴趣"只拉黑该场次 id，不波及母活动）
    def _parent_filtered(aid: str) -> bool:
        pid = (by_id.get(aid)
               or (snapshots.get(aid) or {}).get("activity") or {}).get("pid")
        return bool(pid) and (pid in blocked or pid in excl)

    watch_live = {aid: e for aid, e in watch_state.items()
                  if aid not in blocked and aid not in excl and not _parent_filtered(aid)}

    # ⑥.5 实时名额：列表接口 applyNum 恒 0，关注活动逐个查详情补真数（首次切换同步快照防假事件）。
    # 子项目不查详情——chird 接口每轮自带实时 applyNum 真数（ADR-0015）
    if not args.offline:
        watch_active = [aid for aid in watch_live
                        if aid in by_id and not by_id[aid].get("pid")
                        and _actionable(by_id[aid])]
        if watch_active:
            quota = platform.refresh_quota(watch_active, cfg)
            for aid, q in quota.items():
                a = by_id[aid]
                a["quota_total"], a["quota_left"] = q["quota_total"], q["quota_left"]
                snap = snapshots.get(aid)
                if snap and not snap.get("detail_quota"):
                    snap["detail_quota"] = True
                    if isinstance(snap.get("activity"), dict):
                        snap["activity"]["quota_total"] = q["quota_total"]
                        snap["activity"]["quota_left"] = q["quota_left"]
            log.info(f"[名额] 详情刷新 {len(quota)}/{len(watch_active)} 个关注活动")

    # ⑦ 动向（只对关注清单产生；黑名单活动除外）
    for e in develop.detect(watch_live, snapshots, by_id, now, params, reported):
        _fire(e)

    # ⑧ 参加提醒即时推送（时间敏感，不受推送时点/预算限制——2026-09-22）；其余交预算仲裁
    urgent = [e for e in events if e.get("type") == "attend"]
    events = [e for e in events if e.get("type") != "attend"]
    requeue, sent_keys = push.send_attend_now(urgent, now, args.dry_run)
    queue = queue + requeue
    registry.update({k: now_iso for k in sent_keys})

    to_send, queue, notes = budget.plan(events, queue, reports, now, params)
    for n in notes:
        log.info(f"[仲裁] {n}")
    sent_count = 0
    if to_send:
        # 调档生效当轮才带尾行（spec 决策 10）：不是事件——不占编号/预算，装不下整行丢
        title, body, included, numbers = fmt.render(to_send, now, note=curation_note)
        overflow = [e for e in to_send if e not in included]
        if args.dry_run:
            print(f"\n【演练·不真发】{title}\n{body}\n")
            log.info(f"[演练] 将推送：{title}" +
                     (f"（{len(overflow)} 条顺延）" if overflow else ""))
        else:
            ok, sent_note = push.send_markdown(title, body, to_send, note=curation_note)
            if ok:
                budget.register(reports, included, now)  # 只登记实际发出的
                sent_count = len(included)
                save_mentions(numbers, now_iso)  # 编号映射→反馈指认依据（V2a）
                if overflow:  # 装不下的整条顺延，绝不截断丢失（2026-09-22）
                    queue = queue + overflow
                    log.info(f"[仲裁] 汇总超长，{len(overflow)} 条顺延到下轮")
                log.info(f"[推送] {title}" + (f"（{sent_note}）" if sent_note else ""))
            else:
                log.error(f"[推送] 失败：{sent_note}；{len(to_send)} 条事件回待发队列")
                queue = queue + to_send

    # ⑨ 快照更新 + 90 天清理 + 落盘（演练模式一律不写）
    for a in activities:
        snap = snapshots.get(a["id"])
        if snap is None:
            decide(snapshots, a, None, now_iso)  # 防御：漏网新活动也入库（待评分）
        else:
            snap["activity"] = a
            snap["last_seen"] = now_iso
    pruned = cleanup(snapshots, watch_state, reports, now, params["history_keep_days"])
    if pruned:
        log.info(f"[清理] 移除 {pruned} 条超龄记录（>{params['history_keep_days']} 天）")
    if not args.dry_run:
        save_all(snapshots, watch_state, queue, reports)
        log.info(f"=== 巡检结束：事件 {len(events)}，发送 {sent_count}，在队 {len(queue)} ===")
    return 0


# ------- 打分链换料装配（V3 04 票，ADR-0013 spec 决策 1/7/16） -------

def scoring_inputs(profile: dict | None, feedback_log: list, cfg: dict,
                   now) -> tuple[str, list[str], list[str]]:
    """偏好装配的切换判据（04 票红线）：偏好库在手（load_profile 非 None——
    含"存在但损坏当未初始化"的 01 票口径）→ 打分链换料：偏好段+流水段渲染
    文本、生效关键词/排除词 = 种子 ∪ 活数组并集；偏好库缺席 → 回退旧偏好
    档案——文本原样、词表照旧行解析，与换料前行为逐字节一致（删除偏好库
    文件即整体回退，与飞书通道"摘配置即回退"同款零配置切换）。返回
    (prefs_text, keywords, excludes)——打分模块只收成品，不感知偏好库结构。"""
    if profile:
        return (curate.render_scoring_prefs(profile, feedback_log,
                                            cfg.get("curation") or {}, now),
                curate.effective_keywords(profile),
                curate.effective_excludes(profile))
    prefs_text = read_text(BASE / "preferences.md")
    return (prefs_text, scoring.extract_keywords(prefs_text),
            scoring.extract_excludes(prefs_text))  # 排除词（ADR-0011）


# ------- V3 调档链编排（03 票，ADR-0013 spec 决策 3/4/5/9/13/18） -------

def run_curation(profile: dict | None, feedback_log: list, signup_feed: list,
                 snapshots: dict, activities: list, watch_state: dict, cfg: dict,
                 now, log, call=None, dry_run: bool = False) -> tuple[dict | None, str]:
    """调档链一杆到底：触发水位 → 证据装配 → 调档提示词 → LLM → 容错解析 →
    02 裁决五道闸 → 应用前快照 → 偏好库/关注落盘 → 决策日志。任何环节失败：
    不落盘、只记日志、下轮重试（spec 决策 13）。

    成功返回 (变更集, "")——变更集 = 02 票 adjudicate 的产物（新偏好库 +
    watch_add/watch_remove），05 票用它换打分链的料并汇总尾行；未触发/失败
    返回 (None, 原因行)——原因行即决策日志里"为什么没动"。

    signup_feed：持久累计的报名增删流水 [{"title","action","at"},…]（只追加，
    len 即累计计数器/水位；05 票接线维护）。feedback_seq 取流水最大 seq。
    call：注入罐头回复的测试缝（缺省走网络）。dry_run：全链路照走、不落盘
    （与打分演练同口径——演练看得见调档会做什么，但绝不写状态）。"""
    if not profile:
        return None, "偏好库不存在——调档整体跳过（spec 决策 16）"
    cur_cfg = cfg.get("curation") or {}
    now_iso = now.isoformat(timespec="minutes")
    fb_seq = max((int(e.get("seq") or 0) for e in feedback_log or []), default=0)
    ok, why = curate.should_curate(profile, fb_seq, len(signup_feed or []),
                                   now.date().isoformat())
    if not ok:
        log.info(f"[调档] {why}")
        return None, why
    log.info(f"[调档] 触发：{why}")
    since = (signup_feed or [])[curate.last_watermark(profile)[1]:]  # 上次调档以来
    llm_watch = [{"activity_id": aid, "title": e.get("title", "")}
                 for aid, e in watch_state.items()
                 if "llm" in (e.get("sources") or [])]
    not_started = [{"activity_id": a["id"], "title": a.get("title", ""),
                    "start_time": a.get("start_time", "")}
                   for a in activities
                   if (start := parse_dt(a.get("start_time"))) and start > now]
    request, why = curate.curate_request(
        profile, feedback_log, since, snapshots, llm_watch, not_started,
        cur_cfg, now, cfg.get("llm"), call=call)
    if request is None:
        log.error(f"[调档] {why}——档案原样，下轮重试")
        return None, why
    changeset, why = curate.adjudicate(
        profile, request, {a["id"] for a in activities},
        {c["activity_id"] for c in not_started}, watch_state,
        {"keyword_step": cur_cfg.get("keyword_step", 3),
         "watch_step": cur_cfg.get("watch_step", 2)},
        now_iso, {"feedback_seq": fb_seq, "signup_changes": len(signup_feed or [])})
    if changeset is None:
        log.error(f"[调档] 申请单作废：{why}"
                  + (f"（模型理由：{str(request.get('rationale', ''))[:80]}）"
                     if request.get("rationale") else ""))
        return None, why
    rec = changeset["profile"]["curations"][-1]
    ap = rec["applied"]
    n = (len(ap["kw_add"]), len(ap["kw_remove"]),
         len(ap["watch_add"]), len(ap["watch_remove"]))
    summary = f"关键词 +{n[0]} −{n[1]}，关注 +{n[2]} −{n[3]}"
    changeset["note"] = f"今日档案调整：{summary}" if any(n) else ""  # 05 票：汇总尾行
    if dry_run:
        log.info(f"[调档] [演练] 将应用：{summary}（演练不落盘）——{rec['rationale']}")
        return changeset, ""
    stamp = snapshot_curation(cur_cfg.get("snapshot_keep", 30), now=now)
    if stamp:
        log.info(f"[调档] 应用前快照：backups/{stamp}（偏好库+关注清单，可回滚）")
    try:
        save_profile(changeset["profile"])
    except ValueError as e:  # 防御：裁决层产物按构造合法，此处不应发生
        log.error(f"[调档] 偏好库落盘被拒（{e}）——档案原样，下轮重试")
        return None, f"偏好库落盘被拒：{e}"
    for line in watch.remove_llm(watch_state, changeset["watch_remove"], now_iso):
        log.info(f"[调档] {line}")
    for line in watch.add_llm(watch_state, changeset["watch_add"], now_iso):
        log.info(f"[调档] {line}")
    if changeset["watch_add"] or changeset["watch_remove"]:
        try:
            save_json("watch.json", watch_state)
        except OSError as e:  # 偏好库已应用：不回滚半途而废，大声记日志留恢复线索
            log.error(f"[调档] 关注清单落盘失败（{e!r}）——偏好库已应用"
                      + (f"，可从 backups/{stamp} 快照回滚" if stamp else ""))
    log.info(f"[调档] 应用：{summary}——{rec['rationale']}")
    return changeset, ""
