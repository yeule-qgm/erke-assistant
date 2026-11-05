"""L1 支撑层：状态文件读写、巡检互斥锁与日期小工具。

2026-09-22 从 patrol.py 拆出（行数纪律：核心 ≤150、入口/编排 ≤250）。放在顶层而非
core/ 包内——本模块服务于编排层的运输细节，core/ 必须保持零运输依赖（ADR-0005⑤）。
状态文件人可读、可手改（官方调试手段，见 docs/testing-v1.md）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "state"
LOG = logging.getLogger("agent")
LOCK = STATE / ".patrol.lock"
LOCK_STALE_SECONDS = 600  # 锁超过 10 分钟视为上次巡检崩溃残留，允许接管


def set_state_dir(path: Path) -> None:
    """切换状态目录（2026-09-28）：--offline 演练走 state-offline/，与真实 state/ 完全隔离，
    "离线污染真实基线"从靠人工备份恢复变为结构性不可能。"""
    global STATE, LOCK
    STATE = Path(path)
    LOCK = STATE / ".patrol.lock"


# ---------------- 巡检互斥锁 ----------------

def acquire_lock() -> bool:
    """原子创建锁文件；已有锁则本次放弃（等下一小时）。锁龄超限视为崩溃残留，接管。"""
    STATE.mkdir(exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - LOCK.stat().st_mtime > LOCK_STALE_SECONDS:
                LOG.warning(f"发现超过 {LOCK_STALE_SECONDS}s 的残留锁，判定为崩溃残留，接管")
                LOCK.unlink()
                continue
            LOG.info("已有巡检在运行，本次放弃（下一小时自动重试）")
            return False
    return False


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


# ---------------- 状态读写 ----------------

def load_json(name: str, default):
    p = STATE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        LOG.error(f"{name} 损坏，当作不存在重建（默认值）")
        return default


def save_json(name: str, data) -> None:
    STATE.mkdir(exist_ok=True)
    atomic_write(STATE / name, data)


def save_all(snapshots: dict, watch_state: dict, queue: list, reports: dict) -> None:
    save_json("activities.json", snapshots)
    save_json("watch.json", watch_state)
    save_json("queue.json", queue)
    save_json("reports.json", reports)


def save_mentions(numbers: dict, now_iso: str) -> None:
    """编号→活动映射（用户反馈的指认依据，48h 窗口在解析侧判）。
    仅巡检写（单一写者）：每次推送整表覆盖——mentions 语义就是"最近一条推送"。"""
    save_json("mentions.json", {n: {"activity_id": e["activity_id"],
                                    "title": e.get("title", ""), "at": now_iso}
                                for n, e in numbers.items()})


def atomic_write(path: Path, data) -> None:
    """先写临时文件再改名——巡检中途断电不留半文件（测试 T12 靠它）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def decide(snapshots: dict, activity: dict, decision, now_iso: str) -> None:
    """新建或更新一个活动的快照条目。decision=None 表示待评分（下次重试）。"""
    snap = snapshots.get(activity["id"])
    if snap is None:
        snapshots[activity["id"]] = {"first_seen": now_iso, "last_seen": now_iso,
                                     "activity": activity, "decision": decision}
    else:
        snap["activity"] = activity
        snap["last_seen"] = now_iso
        if decision is not None:
            snap["decision"] = decision


def cleanup(snapshots: dict, watch: dict, reports: dict, now, keep_days: int) -> int:
    """90 天保留期：清理超龄快照（连同其关注条目）与纯关注条目（2026-09-22 补漏）。"""
    cutoff = now - timedelta(days=keep_days)
    stale = [aid for aid, s in snapshots.items()
             if (parse_dt(s.get("last_seen") or s["first_seen"]) or now) < cutoff]
    stale_watch = [aid for aid, e in watch.items()
                   if aid not in snapshots
                   and (parse_dt(e.get("added") or "") or now) < cutoff]
    for aid in stale + stale_watch:
        snapshots.pop(aid, None)
        watch.pop(aid, None)
    registry = reports.setdefault("events", {})
    gone = set(stale) | set(stale_watch)
    for k in [k for k in registry if k.split(":")[0] in gone]:
        del registry[k]
    return len(stale) + len(stale_watch)


# ---------------- 小工具 ----------------

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def load_themes(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


# ------- 反馈落盘小工具（2026-10-18 从 feedback_server 拆入，行数纪律） -------

def update_blocklist(r: dict) -> None:
    """不感兴趣（action=="block"）→ 进黑名单；感兴趣 → 移出（反复反馈最后一次为准）。"""
    bl = load_json("blocklist.json", {})
    if r["action"] == "block":
        bl[r["activity_id"]] = {"title": r["title"],
                                "at": datetime.now().isoformat(timespec="minutes")}
    else:
        bl.pop(r["activity_id"], None)
    save_json("blocklist.json", bl)


def append_feedback_fact(title: str, action: str, when: str) -> None:
    """偏好档案"反馈记录"章节只追加一行事实（ADR-0005 接缝②：永不改手写区）。
    路径走 preferences_md()（2026-11-04）：生产 = 根目录旧档，沙箱/--offline =
    状态目录内夹具副本——反馈演练绝不碰真实档案（决策 15 同款隔离）。调用闸
    在 feedback_server（偏好库在场即不再追加，spec 决策 16/24）；本工具无条件
    追加——无偏好库时它是回退链路的活调用方。"""
    path = preferences_md()
    if not path.exists():
        return  # 档案未建立（骨架期）——流水在 feedback.json，事实不丢
    text = path.read_text(encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        if "## 反馈记录" not in text:
            f.write("\n\n## 反馈记录（机器只追加，本节勿手改）\n\n")
        f.write(f"- {when} 反馈：《{title}》→ {'不感兴趣' if action == 'block' else '感兴趣'}\n")


def load_config(section: str, path: Path | None = None) -> dict:
    """config.toml 的某一节（各模块共用的读取口径，2026-10-19 五处副本收敛）。
    读不到或该节不是表 → 空表；默认值与范围校验归各调用方
    （agent._load_config 是带全量校验的另一口径，勿混）。"""
    try:
        import tomllib
        with open(path or BASE / "config.toml", "rb") as f:
            v = tomllib.load(f).get(section, {})
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def load_params() -> dict:
    """config.toml [params] 节（反馈服务与巡检共用的读取口径）。"""
    return load_config("params")


# ------- 偏好库读写（V3，ADR-0013；schema 见 .scratch spec 实施决策 2，01 票） -------

PROFILE = "llm_profile.json"


def preferences_md() -> Path:
    """旧偏好档案路径：默认状态目录 → 根目录 preferences.md；--offline 沙箱
    （state-offline/ 等）→ 沙箱内夹具副本（prepare_fixture 复制）——初始化
    两段式的改名/覆盖只允许发生在沙箱里，演练绝不碰真实档案。"""
    return BASE / "preferences.md" if STATE == BASE / "state" else STATE / "preferences.md"


def prepare_fixture(source: Path) -> bool:
    """把 source 复制为沙箱夹具 preferences.md（--offline 演练零准备）。
    非沙箱 / 夹具已存在 / 源不存在 → 不动，返回 False。"""
    dst = STATE / "preferences.md"
    if STATE == BASE / "state" or dst.exists() or not source.exists():
        return False
    shutil.copyfile(source, dst)
    return True


def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def profile_errors(p) -> list[str]:
    """偏好库 schema 校验。返回错误清单，空列表 = 合法。
    手改友好的宽松度：多余键宽容，缺键/类型错/枚举外 owner 报错；
    excludes 条目只认 owner:user——LLM 不可写排除词是立法红线（ADR-0013），
    结构即护栏：schema 不合法拒绝落盘（save_profile）。"""
    if not isinstance(p, dict):
        return ["偏好库根必须是 JSON 对象"]
    errs: list[str] = []
    if p.get("schema") != 1:
        errs.append("schema 必须为 1")
    seed = p.get("seed")
    if not isinstance(seed, dict):
        errs.append("seed 必须为对象")
    else:
        if not isinstance(seed.get("copied_at"), str):
            errs.append("seed.copied_at 必须为字符串")
        if not isinstance(seed.get("text"), str):
            errs.append("seed.text 必须为字符串")
        if not _str_list(seed.get("keywords")):
            errs.append("seed.keywords 必须为字符串数组")
        if not _str_list(seed.get("excludes")):
            errs.append("seed.excludes 必须为字符串数组")
    if not isinstance(p.get("understanding"), str):
        errs.append("understanding 必须为字符串")
    for name, owners in (("keywords", ("llm", "user")), ("excludes", ("user",))):
        arr = p.get(name)
        if not isinstance(arr, list):
            errs.append(f"{name} 必须为数组")
            continue
        for i, e in enumerate(arr):
            if not isinstance(e, dict) or not isinstance(e.get("word"), str) or not e.get("word"):
                errs.append(f"{name}[{i}] 缺合法 word")
                continue
            if e.get("owner") not in owners:
                errs.append(f"{name}[{i}].owner 必须为 {' 或 '.join(owners)}（白名单写权限）")
            if not isinstance(e.get("added_at"), str):
                errs.append(f"{name}[{i}].added_at 必须为字符串")
            if not _str_list(e.get("evidence", [])):
                errs.append(f"{name}[{i}].evidence 必须为字符串数组")
    corr = p.get("corrections")
    if not isinstance(corr, list):
        errs.append("corrections 必须为数组")
    else:
        for i, e in enumerate(corr):
            if not isinstance(e, dict):
                errs.append(f"corrections[{i}] 必须为对象")
                continue
            for k in ("at", "about", "finding"):
                if not isinstance(e.get(k), str):
                    errs.append(f"corrections[{i}].{k} 必须为字符串")
            if not _str_list(e.get("evidence", [])):
                errs.append(f"corrections[{i}].evidence 必须为字符串数组")
    cur = p.get("curations")
    if not isinstance(cur, list):
        errs.append("curations 必须为数组")
    else:
        for i, e in enumerate(cur):
            if not isinstance(e, dict):
                errs.append(f"curations[{i}] 必须为对象")
                continue
            for k in ("at", "trigger", "rationale"):
                if not isinstance(e.get(k), str):
                    errs.append(f"curations[{i}].{k} 必须为字符串")
            if not isinstance(e.get("applied"), dict):
                errs.append(f"curations[{i}].applied 必须为对象")
            wm = e.get("watermark")
            if not (isinstance(wm, dict)
                    and all(isinstance(wm.get(k), int) and not isinstance(wm.get(k), bool)
                            for k in ("feedback_seq", "signup_changes"))):
                errs.append(f"curations[{i}].watermark 必须含整数 feedback_seq/signup_changes")
    return errs


def save_profile(profile: dict) -> None:
    """偏好库落盘：schema 校验不过拒绝写（ValueError，01 票红线），
    合法才走原子写（tmp+rename，save_json）。"""
    errs = profile_errors(profile)
    if errs:
        raise ValueError("偏好库 schema 不合法，拒绝落盘：" + "；".join(errs))
    save_json(PROFILE, profile)


def load_profile() -> dict | None:
    """读偏好库。不存在 → None（V3 切换判据 = 文件存在性，spec 决策 16）；
    存在但 JSON 损坏或 schema 不合法 → 也返回 None 并告警——安全默认 =
    当作未初始化、回退旧档案链路（与"删除文件即回退"同款语义）。"""
    p = STATE / PROFILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        LOG.error(f"{PROFILE} JSON 损坏，当作未初始化（回退旧档案链路）")
        return None
    errs = profile_errors(data)
    if errs:
        LOG.error(f"{PROFILE} schema 不合法（{'；'.join(errs)}），当作未初始化")
        return None
    return data


def profile_exists() -> bool:
    """偏好库文件是否存在（存在性 = 切换判据；损坏与否不在此判断）。"""
    return (STATE / PROFILE).exists()


# ------- 调档快照与回滚载体（V3 03 票；spec 决策 9：应用前备份，留 N 份） -------

def snapshot_curation(keep=30, now: datetime | None = None) -> str:
    """调档应用前快照：偏好库 + 关注清单拷入 state/backups/<时间戳>/ 子目录
    （回滚目标 = 最新一份，05 票消费）。超过 keep 份时最老的先删；keep 坏值
    回退缺省 30。偏好库不存在 → 返回 ""（无库可快照——防御分支，链路在偏好
    库已加载时才走到这）。目录名 %Y%m%dT%H%M%S 字典序即时间序；先写 .tmp
    目录再改名，崩溃不留顶着正式名的半份快照。"""
    try:
        keep = max(1, int(keep))
    except (TypeError, ValueError):
        keep = 30
    src_profile, src_watch = STATE / PROFILE, STATE / "watch.json"
    if not src_profile.exists():
        return ""
    backups = STATE / "backups"
    backups.mkdir(exist_ok=True)
    for stale in backups.glob("*.tmp"):  # 上次崩溃残留（单写者，无并发竞态）
        shutil.rmtree(stale, ignore_errors=True)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    tmp = backups / f"{stamp}.tmp"
    tmp.mkdir()
    shutil.copyfile(src_profile, tmp / PROFILE)
    if src_watch.exists():
        shutil.copyfile(src_watch, tmp / "watch.json")
    dst = backups / stamp
    if dst.exists():  # 同秒重入（每日一次上限下罕见；测试注入同刻 now 会撞）
        shutil.rmtree(dst)
    tmp.rename(dst)
    olds = _backup_stamps()
    for name in olds[:len(olds) - keep] if len(olds) > keep else []:
        shutil.rmtree(backups / name, ignore_errors=True)
    return stamp


# ------- 回滚请求载体（V3 05/06 票；ADR-0013 决定 10：口令登记文件，巡检单写者恢复） -------

ROLLBACK_REQUEST = "rollback_request.json"


def register_rollback(at: datetime | None = None, note: str = "") -> None:
    """『撤销调档』口令登记（06 票写侧；消费归 process_rollback/巡检首步）。
    幂等：重复口令覆盖同一文件（时间戳刷新），不堆积——巡检消费的是"有请求"
    这一事实。note 只当审计附注进回滚后的 curations 记录（读侧截 60 字）。"""
    save_json(ROLLBACK_REQUEST, {
        "at": (at or datetime.now()).isoformat(timespec="minutes"),
        "note": str(note)[:60]})


def rollback_requested() -> bool:
    """『撤销调档』请求文件是否存在（06 票由反馈服务登记；消费归巡检首步，
    写权限保持单写者——反馈服务不直接碰偏好库与关注清单）。"""
    return (STATE / ROLLBACK_REQUEST).exists()


def _backup_stamps() -> list[str]:
    """backups/ 下的快照时间戳清单（字典序即时间序；.tmp 残留不算——
    snapshot_curation 开工先清）。快照与回滚消费共用同一口径。"""
    backups = STATE / "backups"
    if not backups.exists():
        return []
    return sorted(p.name for p in backups.iterdir()
                  if p.is_dir() and not p.name.endswith(".tmp"))


def process_rollback(now: datetime) -> tuple[bool, str]:
    """消费回滚请求：恢复 backups/ 最新一份快照（偏好库 + 关注清单）→ 删请求
    文件。返回 (恢复了?, 日志行——进决策日志，"出问题先看日志"）。请求文件内容
    只当审计附注（可带 note），格式由 06 票定义、此处不依赖。

    恢复成功后在 curations 追加一条回滚记录，一举两得：① 账本连续——撤销也是
    一次档案变更，留下可解释的痕迹；② 吃掉水位——被撤销那轮调档消费过的证据
    不再重触发（否则恢复后水位倒退、同日重跑调档，模型拿同一批证据大概率把
    撤销的东西调回来，user story 15 的"一键退货"形同虚设）。新证据（新反馈/
    新报名增删）照常触发下一轮。

    次序：先校验快照、原子写回（save_profile/save_json，铁律），全部成功才删
    请求——中途崩溃请求仍在，下轮重做（恢复幂等），『撤销调档』指令不丢；
    快照缺失/损坏 → 不恢复、请求作废（大声记日志）。偏好库不在场（决策 16
    零配置回退态：删文件即回退）→ 撤销无对象，请求同样作废——绝不从快照
    复活用户已删除的偏好库。"""
    req = STATE / ROLLBACK_REQUEST
    req_note = str((load_json(ROLLBACK_REQUEST, {}) or {}).get("note") or "")[:60]
    if not profile_exists():
        req.unlink(missing_ok=True)
        return False, "偏好库不在场（决策 16 回退态）——『撤销调档』无对象，请求作废"
    stamps = _backup_stamps()
    if not stamps:
        req.unlink(missing_ok=True)
        return False, "回滚请求已消费，但没有任何调档快照可恢复——请求作废（无档可回）"
    stamp = stamps[-1]
    src = (STATE / "backups") / stamp
    try:
        data = json.loads((src / PROFILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        req.unlink(missing_ok=True)
        return False, f"最近快照（{stamp}）偏好库读不出（{e!r}）——未恢复，请求作废"
    errs = profile_errors(data)
    if errs or not isinstance(data.get("curations"), list):
        req.unlink(missing_ok=True)
        return False, (f"最近快照（{stamp}）偏好库 schema 不合法"
                       f"（{errs[:1]}）——未恢复，请求作废")
    fb = load_json("feedback.json", [])
    seq = max((int(e.get("seq") or 0) for e in fb if isinstance(e, dict)), default=0)
    chg = len(load_json("signup_feed.json", {}).get("entries") or [])
    data["curations"].append({
        "at": now.isoformat(timespec="minutes"),
        "trigger": f"用户『撤销调档』：恢复快照 backups/{stamp}",
        "applied": {"rollback": stamp},
        "rationale": ("用户口令撤销最近一次调档，偏好库与关注清单回滚到应用前"
                      + (f"；附注：{req_note}" if req_note else "")),
        "watermark": {"feedback_seq": seq, "signup_changes": chg}})
    try:
        save_profile(data)  # 原子写（tmp+rename），schema 闸已过——防御性兜底
    except ValueError as e:
        req.unlink(missing_ok=True)
        return False, f"回滚记录落盘被拒（{e}）——未恢复，请求作废，请人工核对快照"
    if (src / "watch.json").exists():
        try:
            save_json("watch.json", json.loads((src / "watch.json")
                                               .read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:  # 快照关注清单坏：偏好库已恢复，不半途而废
            LOG.error(f"[回滚] 快照（{stamp}）关注清单读不出（{e!r}）——偏好库已恢复，"
                      "关注清单保持现状")
    req.unlink(missing_ok=True)
    return True, f"已恢复最近快照 backups/{stamp}（偏好库+关注清单），并记一条回滚调档（吃掉水位）"
