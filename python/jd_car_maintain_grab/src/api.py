"""在活动页内直接调用京东兑换接口，替代「点立即兑换 → 点确认兑换」的模拟操作。

为什么换：按钮模拟要求目标元素真正可见、可点击、且没被遮罩挡住。实测在当前
SSR + 弹窗结构下很不稳定——`locator.click()` 会超时，`click(force=True)` 虽然
报告点击成功，却一个请求都没发出。直调接口只要求页面已加载。

参数从哪来：兑换接口需要的业务参数由页面自身的数据提供（`window.__react_data__`
里每个 sku 的 activityWareId / activityId / exchangeScore / exchangeWareType），
风控参数由页面原生的 `window.getJdEid()` 与 `window.getJsToken()` 提供。因此执行
环境仍是那个已登录的真实 Chrome，Python 侧不需要复刻任何签名。

参数什么时候能取到：**首屏 HTML 里没有**这些参数（实测 SSR HTML 中
`activityWareId`/`exchangeScore` 出现 0 次），它们是页面打开后异步拉取活动数据
（floor）才写进 `window.__react_data__` 的，所以刚 open 页面时读不到，需要轮询
等待。取参数**与库存无关**：已抢完（`skuStatus=5`）的商品同样能读到完整参数，
因此不需要等 10:00 补库存就能提前读到并缓存。

实测结论（2026-09-11，functionId=bff_rights_points_exchange）：
  - 不带 h5st 也能通过校验；
  - 但 body 必须携带 jsToken，否则返回 F30001「您操作频率过快」；
  - 参数正确时返回业务码，如 1711000 成功、1711001「当前参与人数过多」。
"""

import json
import time

from . import config
from .browser import log

SKU_POLL_MS = 500  # 等待页面活动数据加载时的轮询间隔

# 从 window.__react_data__ 里按 skuId 取出该商品的兑换参数。
# __SKU_ID__ 会被替换成实际 skuId（来自 config，纯数字，拼进 JS 字面量安全）。
_JS_READ_SKU = """(() => {
  const skuId = "__SKU_ID__";
  let data = '';
  try { data = JSON.stringify(window.__react_data__ || ''); } catch (e) { return null; }
  const at = data.indexOf('"skuId":"' + skuId + '"');
  if (at < 0) return null;
  const seg = data.slice(at, at + 1200);
  const num = (key) => {
    const m = seg.match(new RegExp('"' + key + '":([0-9]+)'));
    return m ? Number(m[1]) : null;
  };
  const str = (key) => {
    const m = seg.match(new RegExp('"' + key + '":"([^"]*)"'));
    return m ? m[1] : '';
  };
  // 值为 null 的字段（如 btnRedirectUrl）取出来是空串
  const nullable = (key) => {
    const m = seg.match(new RegExp('"' + key + '":("([^"]*)"|null)'));
    return m ? (m[2] || '') : '';
  };
  // 商品名在 skuInfos 之前，取 skuId 之前最近的一个 name，便于日志确认抢的是哪款
  const head = data.slice(0, at);
  const nm = head.lastIndexOf('"name":"');
  return {
    skuId: skuId,
    name: nm < 0 ? '' : head.slice(nm + 8, head.indexOf('"', nm + 8)),
    exchangeScore: num('exchangeScore'),
    activityWareId: str('activityWareId'),
    activityId: num('activityId'),
    exchangeWareType: num('exchangeWareType'),
    skuStatus: num('skuStatus'),
    btnRedirectUrl: nullable('btnRedirectUrl'),
  };
})()"""

# 在页面内拼装表单并发起兑换请求。body 里的 eid/fp/jsToken/sdkToken 每次现取，
# 避免 token 过期。__BODY__ / __FUNCTION_ID__ / __URL__ 会被替换成实际值。
_JS_EXCHANGE = """(async () => {
  const body = __BODY__;
  const eid = (window.getJdEid && window.getJdEid()) || {};
  const token = await new Promise((done) => {
    if (window.getJsToken) { window.getJsToken(done); } else { done({}); }
  });
  const form = {
    appid: 'plus_business',
    functionId: '__FUNCTION_ID__',
    body: JSON.stringify(Object.assign({}, body, {
      eid: eid.eid || '',
      fp: eid.fp || '',
      jsToken: (token && token.jsToken) || '',
      sdkToken: (token && token.sdkToken) || '',
    })),
    loginType: '2',
    loginWQBiz: '',
    xAPIClientLanguage: 'zh_CN',
    scval: '',
  };
  const resp = await fetch('__URL__', {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams(form).toString(),
  });
  return await resp.text();
})()"""


def read_sku_params(page, sku_id: str, timeout_ms: int = 30000) -> dict | None:
    """等页面活动数据加载出来，读取该 sku 的兑换参数；超时仍读不到返回 None。

    参数来自页面异步拉取的活动数据（首屏 HTML 里没有），所以刚打开页面时可能
    读不到，这里轮询等待。与库存状态无关——已抢完的商品同样能读到完整参数。
    """
    deadline = time.time() + timeout_ms / 1000
    while True:
        try:
            sku = page.evaluate(_JS_READ_SKU.replace("__SKU_ID__", sku_id))
        except Exception as exc:
            log(f"读取兑换参数失败: {exc}")
            return None
        if sku and sku.get("activityWareId") and sku.get("activityId"):
            return sku
        if time.time() >= deadline:
            return None
        page.wait_for_timeout(SKU_POLL_MS)


def exchange(page, sku: dict, service_type: str = "jdxc") -> dict:
    """在活动页内直接发起一次兑换，返回响应字典（成功时含 code/msg/rs）。"""
    body = {
        "scene": "serviceExchange",
        "skuId": sku["skuId"],
        "serviceType": service_type,
        "activityWareId": sku["activityWareId"],
        "exchangeScore": str(sku["exchangeScore"]),
        "activityId": sku["activityId"],
        "exchangeWareType": sku["exchangeWareType"],
        "sourceCode": "",
    }
    expression = (_JS_EXCHANGE
                  .replace("__BODY__", json.dumps(body, ensure_ascii=False))
                  .replace("__FUNCTION_ID__", config.EXCHANGE_FUNCTION_ID)
                  .replace("__URL__", config.EXCHANGE_URL))
    try:
        raw = page.evaluate(expression)
    except Exception as exc:
        return {"code": "PageError", "msg": str(exc)}
    try:
        return json.loads(raw)
    except Exception:
        return {"code": "ParseError", "msg": str(raw)[:200]}
