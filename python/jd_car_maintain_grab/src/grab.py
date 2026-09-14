"""京东「免费小保养」抢购模块。

10:00:00 到点后，直接在活动页内调用兑换接口 `bff_rights_points_exchange`，
按业务码判断结果并重试，不模拟点击：既不依赖按钮渲染、遮罩遮挡与弹窗时机，
也不需要在每轮之间刷新整页。

兑换参数（activityWareId / activityId / exchangeScore ...）来自页面打开后
异步拉取的活动数据，因此脚本启动时会先等到参数可读，再等到点开抢。

脚本在到点前启动会等到点；若启动时已过设定时刻，则不再等待、立即开始，
因此盘中手动补跑或试跑都是直接发请求。

登录需在绑定的真实 Chrome（--cdp）中手动完成；「受信任会话」下页面才会
返回该商品完整的兑换参数。
"""

import argparse
import sys
import time
import traceback
from datetime import datetime

from . import api, config
from .browser import cdp_browser, ensure_logged_in, log

# ---- 抢购参数 ----
API_ATTEMPTS = 20              # 到点后调用兑换接口的次数
API_INTERVAL_MS = 1000         # 相邻两次调用的间隔（400ms 会触发 F30001 限流）
API_COOLDOWN_MS = 3000         # 被限流（F30001）时的额外等待
PARAM_WAIT_MS = 30000          # 等待页面活动数据加载出兑换参数的超时
PAGE_LOAD_TIMEOUT_MS = 30000   # 页面加载（goto）超时


def resolve_start(start_str: str | None) -> datetime:
    """返回当天的到点抢购时刻，默认今天 10:00:00（放库存时刻）。

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


def _save_screenshot(page) -> None:
    try:
        page.screenshot(path="grab_result.png")
        log("已保存截图: grab_result.png")
    except Exception:
        pass


def grab_by_api(page, sku: dict) -> int:
    """到点后直接在页面内调用兑换接口，返回退出码。"""
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
        # 被风控限流时多等一会儿，硬打只会继续被拒
        if code == config.EXCHANGE_RATE_LIMIT_CODE:
            page.wait_for_timeout(API_COOLDOWN_MS)
        else:
            page.wait_for_timeout(API_INTERVAL_MS)
    log(f"接口调用 {API_ATTEMPTS} 次均未成功，退出。")
    return 1


def run_grab(page, start_time: datetime, test: bool) -> int:
    """到点后直调兑换接口抢购，返回退出码。"""
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
        log(f"当前 {datetime.now().strftime('%H:%M:%S')} 已过目标时刻 "
            f"{start_time.strftime('%H:%M:%S')}，不再等待，立即开始抢购。")
    else:
        log(f"等待到 {start_time.strftime('%H:%M:%S.%f')[:-3]} 开始抢购...")
        wait_until(start_time)
        log(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} 到点，开始调用兑换接口。")

    if sku is None:
        sku = api.read_sku_params(page, config.SKU_ID, PARAM_WAIT_MS)
    if sku is None:
        log("读不到该商品的兑换参数（页面活动数据可能没加载出来），本次不抢购。")
        return 1

    code = grab_by_api(page, sku)
    _save_screenshot(page)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="京东免费小保养自动抢购")
    parser.add_argument("--start", help="到点抢购时间，格式 HH:MM:SS，默认 10:00:00")
    parser.add_argument("--test", action="store_true", help="立即开始（测试模式）")
    parser.add_argument("--cdp", default="http://localhost:9222",
                        help="已登录的真实 Chrome 调试地址，默认 http://localhost:9222")
    args = parser.parse_args()

    start_time = datetime.now() if args.test else resolve_start(args.start)

    # 运行头尾均落日志，便于定时任务执行后核对“有没有按时跑、结果如何”
    log("=" * 20 + " 京东免费小保养抢购 " + "=" * 20)

    log(f"启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"目标时刻: {start_time.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"test={args.test} attempts={API_ATTEMPTS} cdp={args.cdp}")

    code = 1
    try:
        with cdp_browser(args.cdp) as (page, context):
            log(f"已连接到已登录的真实浏览器（CDP: {args.cdp}）。")
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
