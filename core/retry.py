"""L3 领域层：退避重试小工具（2026-10-19 输出层四处同构循环收敛于此）。

推送通道的失败语义一致（Q23）：立即试一次，
失败按 1/2/4 秒退避共三轮，仍败把错误说明交回上层。网络异常的捕获归各
调用方（自行包成 AttemptFailed），本模块保持零第三方依赖。
"""
import time


class AttemptFailed(Exception):
    """被调方判定本次尝试失败；str(e) 即错误说明，重试耗尽后原样上交。"""


def with_retry(fn, delays=(1, 2, 4)):
    """先立即执行 fn() 一次，失败再按 delays 逐秒退避重试。

    fn() 正常返回 → (返回值, "")；抛 AttemptFailed(msg) → 记下 msg 继续；
    全部失败 → (None, 最后一条 msg)。fn 抛出的其他异常原样向上。
    """
    err = ""
    for delay in (0,) + tuple(delays):
        if delay:
            time.sleep(delay)
        try:
            return fn(), ""
        except AttemptFailed as e:
            err = str(e)
    return None, err
