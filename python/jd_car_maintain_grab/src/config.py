"""集中管理项目常量与 URL。"""

# ---- 抢购目标 ----
# 免费小保养（¥399 / 9 积分）。同活动其它商品，改 SKU_ID 即改目标：
#   京东养车1L保养机油 ¥99 / 1 积分 -> 100240903791
#   汽车保养5折券       ¥399 / 3 积分 -> 1409644046
SKU_ID = "100228358274"

# ---- 页面 URL ----
# 页面按 URL 里的 skuId 决定默认选中哪张商品卡，因此与 SKU_ID 保持一致
ACTIVITY_URL = (
    "https://pro.m.jd.com/mall/active/3Rcw1NV6pjiUBpznNHooXjPNAicD/"
    "index.html?babelChannel=ttt1&categoryId=331801&serviceType=jdxc&skuId=" + SKU_ID
)

# ---- 兑换接口（页面内直调，见 src/api.py）----
EXCHANGE_FUNCTION_ID = "bff_rights_points_exchange"
EXCHANGE_URL = "https://api.m.jd.com/api?functionId=" + EXCHANGE_FUNCTION_ID
EXCHANGE_OK_CODE = "1711000"  # 兑换成功
# 操作频率过快（实测请求间隔 400ms 时大量出现，需退避而不是继续硬打）
EXCHANGE_RATE_LIMIT_CODE = "F30001"
