"""京东「免费小保养」抢购模块（项目唯一入口，含启动 Chrome）。

10:00:00 放库存，脚本等到该时刻就在活动页内调用兑换接口
`bff_rights_points_exchange`：头 BURST_MS 毫秒（默认 2s）按 BURST_INTERVAL_MS
（默认 100ms）密集打——份额基本在这一瞬被抢完——之后回落到 API_INTERVAL_MS
（默认 1s）继续重试。按业务码判断结果，不模拟点击：既不依赖按钮渲染、遮罩遮挡与
弹窗时机，也不需要在每轮之间刷新整页。

不提前开抢：页面数据里的可兑换时段是 10:00:00-23:50:00 与 00:00:00-09:25:00，
09:25-10:00 属关闭时段，提前发请求只会拿到 1714001「请稍后再试」而白烧机会。

兑换参数（activityWareId / activityId / exchangeScore ...）来自页面打开后
异步拉取的活动数据，因此脚本启动时会先等到参数可读，再等放库存时刻。

脚本会等到放库存时刻（10:00:00）；若启动时已过该时刻，则不再等待、立即开始，
因此盘中手动补跑或试跑都是直接发请求。

浏览器：默认自动启动带调试端口的真实 Chrome（脚本自己启动、结束时关闭），并直接
打开活动页——活动数据会随页面一起开始加载。只有检测到未登录（页面被跳到登录页）时
才在该登录页手动完成登录；「受信任会话」下页面才会返回该商品完整的兑换参数。
"""

import argparse
import sys
import time
import traceback
from datetime import datetime

from . import api, config
from .browser import (
    cdp_browser,
    ensure_logged_in,
    launch_chrome,
    log,
    wait_for_cdp,
)

# ---- 抢购参数 ----
BURST_MS = 2000                # 放库存后的密集尝试时间窗（份额基本在此被抢完）
BURST_INTERVAL_MS = 100        # 密集窗口内的间隔
API_ATTEMPTS = 40              # 总调用次数（约：前 2s 内 20 次 + 之后每秒 1 次 × 20）
API_INTERVAL_MS = 1000         # 密集窗口结束后的间隔（长时间 400ms 会触发 F30001 限流）
API_COOLDOWN_MS = 3000         # 被限流（F30001）时的额外等待
PARAM_WAIT_MS = 30000          # 等待页面活动数据加载出兑换参数的超时
PAGE_LOAD_TIMEOUT_MS = 30000   # 页面加载（goto）超时

# ---- 启动 Chrome 参数 ----
DEFAULT_PORT = 9222            # 调试端口
DEFAULT_PROFILE = "./jd_cdp_profile"  # 用户数据目录（登录态保存在这里）
CDP_WAIT_MS = 15000            # 等待 Chrome 调试端口就绪的超时


def resolve_start(start_str: str | None) -> datetime:
    """返回当天的放库存（开抢）时刻，默认今天 10:00:00。

    返回的时刻可能已经过去：此时不再等待，直接开始抢购（便于盘中补跑与
    手动试跑）。
    """
    hh, mm, ss = 10, 0, 0
    if start_str:
        parts = start_str.split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        ss = int(parts[2]) if len(parts) > 2 else 0
    now = datetime.now()
    return now.replace(hour=hh, minute=mm, second=ss, microsecond=0)


def wait_until(target: datetime) -> None:
    """等到目标时刻，毫秒级精度。

    `time.sleep` 在 Windows 上有毫秒级抽动，因此先睡到剩 10ms，最后一段
    用 `perf_counter` 自旋，尽量把误差压到 1ms 以内。
    """
    deadline = time.perf_counter() + (target - datetime.now()).total_seconds()
    while True:
        remain = deadline - time.perf_counter()
        if remain <= 0:
            return
        if remain > 0.01:
            time.sleep(remain - 0.005)


def grab_by_api(page, sku: dict) -> int:
    """到点后直接在页面内调用兑换接口，返回退出码。

    节奏：放库存后的 BURST_MS 时间窗内按 BURST_INTERVAL_MS 密集打，窗口结束后
    回落到 API_INTERVAL_MS——份额基本在开抢那一瞬被抢完，但长时间高频会被风控限流。
    """
    started = time.perf_counter()
    for attempt in range(1, API_ATTEMPTS + 1):
        resp = api.exchange(page, sku)
        code = resp.get("code")
        msg = resp.get("msg") or resp.get("displayMsg") or ""
        if code == config.EXCHANGE_OK_CODE:
            log(f"[{attempt}/{API_ATTEMPTS}] 兑换成功: {msg or resp.get('rs')}")
            return 0
        log(f"[{attempt}/{API_ATTEMPTS}] 未成功: code={code} msg={msg}")
        if attempt >= API_ATTEMPTS:
            break
        elapsed_ms = (time.perf_counter() - started) * 1000
        wait_ms = BURST_INTERVAL_MS if elapsed_ms < BURST_MS else API_INTERVAL_MS
        # 被风控限流时多等一会儿，硬打只会继续被拒
        if code == config.EXCHANGE_RATE_LIMIT_CODE:
            wait_ms = API_COOLDOWN_MS
        page.wait_for_timeout(wait_ms)
    log(f"接口调用 {API_ATTEMPTS} 次均未成功，退出。")
    return 1


def run_grab(page, start_time: datetime, test: bool) -> int:
    """到开抢时刻直调兑换接口抢购，返回退出码。"""
    log("打开活动页...")
    try:
        page.goto(config.ACTIVITY_URL, wait_until="domcontentloaded",
                  timeout=PAGE_LOAD_TIMEOUT_MS)
    except Exception as exc:
        log(f"打开活动页失败: {exc}")
        return 1
    if not ensure_logged_in(page, config.ACTIVITY_URL):
        return 1

    # 兑换参数是页面打开后异步加载的：先等到能读到再等到点，避免到点了才发现
    # 取不到参数。已抢完的商品同样能读到完整参数，因此不用等补库存。
    sku = api.read_sku_params(page, config.SKU_ID, PARAM_WAIT_MS)
    if sku is not None:
        log(f"目标商品: {sku['name'] or '未知'}（skuId={sku['skuId']}，"
            f"{sku['exchangeScore']} 积分，skuStatus={sku['skuStatus']}）")
        if sku.get("btnRedirectUrl"):
            # 这类商品走「拿 sourceTicket → 跳转 m-sep.jd.com/Settlement 提交订单」，
            # 不调兑换接口，直调接口必然失败，提前说清楚而不是白跑 N 次
            log("警告: 该商品的下单流程是跳转结算页（btnRedirectUrl 非空），"
                "不走兑换接口，请改用「免费小保养」等走接口的商品。")
            return 1

    if test:
        log("测试模式：立即开始抢购。")
    elif start_time <= datetime.now():
        log(f"当前 {datetime.now().strftime('%H:%M:%S')} 已过放库存时刻 "
            f"{start_time.strftime('%H:%M:%S')}，不再等待，立即开始抢购。")
    else:
        log(f"等待到 {start_time.strftime('%H:%M:%S.%f')[:-3]} 放库存开抢"
            f"（前 {BURST_MS / 1000:g}s 每 {BURST_INTERVAL_MS}ms 一次，"
            f"之后每 {API_INTERVAL_MS}ms 一次）...")
        wait_until(start_time)
        log(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} 开抢，开始调用兑换接口。")

    if sku is None:
        sku = api.read_sku_params(page, config.SKU_ID, PARAM_WAIT_MS)
    if sku is None:
        log("读不到该商品的兑换参数（页面活动数据可能没加载出来），本次不抢购。")
        return 1

    code = grab_by_api(page, sku)
    return code


def ensure_cdp(cdp_url: str, args) -> bool:
    """启动脚本自己的 Chrome，并等调试端口就绪。

    唯一路径就是「自己启动窗口」——脚本结束时负责关闭，不存在需要连接既有实例的场景。
    """
    if not launch_chrome(args.port, args.profile, args.url, args.chrome):
        return False
    if not wait_for_cdp(cdp_url, CDP_WAIT_MS):
        log(f"等待 {cdp_url} 就绪超时（Chrome 启动失败）。")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="京东免费小保养自动抢购（必要时自动启动带调试端口的 Chrome）")
    parser.add_argument("--start", help="放库存时间，格式 HH:MM:SS，默认 10:00:00"
                                       "（到点即开抢，不提前）")
    parser.add_argument("--test", action="store_true", help="立即开始（测试模式）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"启动/连接的 Chrome 调试端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help=f"Chrome 用户数据目录，默认 {DEFAULT_PROFILE}")
    parser.add_argument("--url", default=config.ACTIVITY_URL,
                        help="启动 Chrome 时打开的地址，默认活动页"
                             "（未登录时京东会自动跳到登录页）")
    parser.add_argument("--chrome", help="Chrome/Edge 可执行文件路径，默认自动查找")
    args = parser.parse_args()

    cdp_url = f"http://localhost:{args.port}"
    start_time = datetime.now() if args.test else resolve_start(args.start)

    # 运行头尾均落日志，便于定时任务执行后核对“有没有按时跑、结果如何”
    log("=" * 20 + " 京东免费小保养抢购 " + "=" * 20)

    log(f"启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"放库存/开抢: {start_time.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"test={args.test} attempts={API_ATTEMPTS} "
        f"burst={BURST_MS / 1000:g}s/{BURST_INTERVAL_MS}ms cdp={cdp_url}")

    code = 1
    try:
        if ensure_cdp(cdp_url, args):
            with cdp_browser(cdp_url) as (page, context):
                log(f"已连接到已登录的真实浏览器（CDP: {cdp_url}）。"
                    "若需登录，请在浏览器窗口中完成。")
                code = run_grab(page, start_time, args.test)
    except KeyboardInterrupt:
        log("运行被中断（Ctrl+C）。")
        code = 130
    except Exception:
        # 定时任务无人值守，异常必须留痕（如 Chrome 未启动、CDP 连接失败）
        log("运行异常终止:\n" + traceback.format_exc().rstrip())
        code = 1

    log(f"运行结束: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 退出码 {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
