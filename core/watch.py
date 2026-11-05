"""L4 领域层：关注清单（关键 20%·关注规则）。

来源可枚举（ADR-0005 接缝③）：theme=主题命中，signup=报名过，rec=偏好推荐（LLM ≥ 打分线），
interest=用户回复"感兴趣"亲自点名（V2a，ADR-0008；由巡检从反馈流水消费——关注仍单一写者），
llm=curator 点名（V3，ADR-0013），chird=系列母活动的子项目（ADR-0015——正当性继承
母活动的关注而非词匹配；用户报名的场次 signup 源叠加，参加提醒随之生效）。

规则（PROJECT 2026-09-20 + 2026-09-22 追加）：
- 主题命中、报名过、偏好推荐、感兴趣的活动都进关注
- 取消报名（不再出现在我的报名里）且非主题命中 → 移出关注（Q22）
"""


def update(watch: dict, theme_hit_ids: list[str], registration_ids: list[str],
           now_iso: str, titles: dict | None = None,
           recommended_ids: list[str] | None = None,
           interest_ids: list[str] | None = None,
           removal_guard: bool = True) -> list[str]:
    """幂等更新关注清单，返回本次变动的日志行。

    titles：活动 id → 标题，填充关注条目（日志可读、排查用）。
    recommended_ids：LLM 打分达标的偏好推荐，同样进关注——名额空缺/跌破阈值
    对它们同样生效（2026-09-22 用户追加：关注或偏好的活动，名额有动静都要说）。
    interest_ids：反馈流水中未消费的"感兴趣"活动（feedback_server 只写流水，
    进关注这一步归巡检——账本单一写者原则）。
    防脏数据护栏：日常巡检新增报名关注超过 SUSPICIOUS_SIGNUPS 时全部撤销并告警；
    首次导入（watch 中尚无任何 signup 条目）例外——批量导入全部历史报名（2026-09-22）。
    移除护栏（2026-09-28，ADR-0006）：单轮要移除的报名关注超过 SUSPICIOUS_REMOVALS 且
    超过现存报名关注的一半时，疑似『我的报名』接口返回残缺名单，全部保留并告警，
    由用户核对后经 config.toml 的 removal_guard=false 放行（只挡一轮，不改判定）。
    """
    logs = []
    reg_set = set(registration_ids)
    for aid in theme_hit_ids:
        _add_source(watch, aid, "theme", now_iso, titles)
    for aid in interest_ids or []:
        if _add_source(watch, aid, "interest", now_iso, titles):
            logs.append(f"关注 +感兴趣『{_title_of(watch, aid)}』")
    for aid in recommended_ids or []:
        if _add_source(watch, aid, "rec", now_iso, titles):
            logs.append(f"关注 +偏好推荐『{_title_of(watch, aid)}』")
    existing_signups_before = sum(1 for e in watch.values()
                                  if "signup" in e.get("sources", []))
    added = [aid for aid in dict.fromkeys(registration_ids)
             if _add_source(watch, aid, "signup", now_iso, titles)]
    initial_import = existing_signups_before == 0 and len(added) > SUSPICIOUS_SIGNUPS
    if len(added) > SUSPICIOUS_SIGNUPS and not initial_import:
        for aid in added:  # 全部撤销本次新增：脏数据不进关注，才不会在活动当天骚扰
            if "theme" not in watch[aid]["sources"] and watch[aid]["sources"] == ["signup"] \
                    and aid not in theme_hit_ids:
                del watch[aid]
        logs.append(f"[警告] 一次巡检新增 {len(added)} 个报名关注（>{SUSPICIOUS_SIGNUPS}），"
                    f"疑似平台脏数据，全部不入关注；建议人工核对『我的报名』页")
    elif initial_import:
        logs.append(f"关注 +历史报名 {len(added)} 个（首次导入，含全部历史参与）")
    elif added:
        for aid in added:
            logs.append(f"关注 +报名『{_title_of(watch, aid)}』")
    removed = [aid for aid, e in watch.items()
               if "signup" in e.get("sources", [])
               and aid not in reg_set
               and "theme" not in e.get("sources", [])]
    signup_total = existing_signups_before + len(added)
    if (removal_guard and len(removed) > SUSPICIOUS_REMOVALS
            and len(removed) * 2 > signup_total):
        logs.append(f"[警告] 一次巡检要移除 {len(removed)}/{signup_total} 个报名关注"
                    f"（超过一半且超过 {SUSPICIOUS_REMOVALS} 条），疑似『我的报名』接口"
                    "返回了残缺名单，本次全部保留不移除；请去平台核对『我的报名』页，"
                    "确认真已取消后把 config.toml 的 removal_guard 改为 false 放行一轮")
        return logs
    for aid in removed:
        removed_log = _title_of(watch, aid)
        del watch[aid]
        logs.append(f"关注 -取消报名 {removed_log}")
    return logs


SUSPICIOUS_SIGNUPS = 5
SUSPICIOUS_REMOVALS = 10


def _add_source(watch: dict, aid: str, source: str, now_iso: str,
                titles: dict | None = None) -> bool:
    entry = watch.setdefault(aid, {"sources": [], "title": "", "added": now_iso})
    if titles and not entry.get("title"):
        entry["title"] = titles.get(aid, "")
    if source not in entry["sources"]:
        entry["sources"].append(source)
        return True
    return False


def add_llm(watch: dict, picks: list[dict], now_iso: str) -> list[str]:
    """初批 llm 源关注写入（V3 初始化命令，ADR-0013 决策 8/11）：只加 llm 源、
    不动既有源与移除逻辑——调档链的增删应用与幅度护栏归 05 票。
    picks: [{"activity_id", "title", "reason"}]（幻觉 id 已由调用方过滤）。"""
    logs = []
    for p in picks or []:
        aid = str(p.get("activity_id", ""))
        if not aid:
            continue
        if _add_source(watch, aid, "llm", now_iso, {aid: p.get("title", "")}):
            logs.append(f"关注 +llm『{p.get('title') or aid}』"
                        + (f"（{str(p['reason'])[:40]}）" if p.get("reason") else ""))
    return logs


def remove_llm(watch: dict, ids: list[str], now_iso: str) -> list[str]:
    """调档链移除 llm 源关注（V3 03 票）：只摘 llm 标签——条目还有别的源
    （signup/theme/rec/interest）就保留，只剩 llm 源则整条删。非 llm 源
    条目不动（裁决层 owner 闸已把关，这里幂等兜底）。与 signup 移除护栏
    计数互不相干（curator 动的是自己的关注，不是用户的报名）。"""
    logs = []
    for aid in ids or []:
        entry = watch.get(aid)
        if not entry or "llm" not in (entry.get("sources") or []):
            continue
        entry["sources"].remove("llm")
        if not entry["sources"]:
            del watch[aid]
            logs.append(f"关注 -llm『{entry.get('title') or aid}』（整条移除）")
        else:
            logs.append(f"关注 -llm『{entry.get('title') or aid}』（其余源保留）")
    return logs


def add_chird(watch: dict, ids: list[str], now_iso: str,
              titles: dict | None = None) -> list[str]:
    """系列子项目进关注（ADR-0015 chird 源）：由巡检 ⑤.5 在新场次入账时调用
    （关注单一写者原则不变）。只加 chird 源、不动既有源——报名后 signup 源由
    watch.update 正常叠加，取消报名的移除语义也沿用现有规则（chird 不豁免）。"""
    logs = []
    for aid in ids or []:
        if _add_source(watch, aid, "chird", now_iso, titles):
            logs.append(f"关注 +新场次『{_title_of(watch, aid)}』")
    return logs


def _title_of(watch: dict, aid: str) -> str:
    return watch.get(aid, {}).get("title") or aid
