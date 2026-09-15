#!/bin/zsh
# 抢购包装脚本：锁住系统不睡 + 在项目根目录执行 python -m src.grab。
#
# 手动试跑（会真的发兑换请求）：
#   ./deploy/macos/run_grab.sh --test
#
# caffeinate 断言只在本次命令运行期间持有：覆盖「启动 Chrome → 等到 09:59:55
# 提前开抢 → 跑满重试次数」整段，跑完自动释放。持续接电源时 -s 才有效，
# 这正符合本目录的部署前提；-d 让显示屏也别睡，顺带避免 App Nap 节流 Chrome。
set -eu

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "找不到 $PYTHON，请先在项目根目录执行 uv sync" >&2
    exit 1
fi

cd "$PROJECT_DIR"
exec /usr/bin/caffeinate -dims "$PYTHON" -m src.grab "$@"
