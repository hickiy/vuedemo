#!/bin/zsh
# 安装 LaunchAgent：每天 09:50 启动抢购（脚本内部等到 09:59:55 提前开抢）。
# 卸载：launchctl bootout gui/$(id -u)/com.jd.grab && rm ~/Library/LaunchAgents/com.jd.grab.plist
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$DIR/../.." && pwd)"
LABEL="com.jd.grab"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

chmod +x "$DIR/run_grab.sh"
mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"   # launchd 写日志要求目录已存在

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$DIR/$LABEL.plist" > "$TARGET"
plutil -lint "$TARGET"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true   # 已加载则先卸载
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E 'state|program' || true

cat <<'EOF'

安装完成。还需要两步才能真正按时抢到：

1) 定时唤醒——launchd 不会唤醒睡眠中的 Mac，需要 sudo：
     sudo pmset repeat wakeorpoweron MTWRFSU 09:45:00
   查看：pmset -g sched

2) 确认这台 Mac 持续接电源（caffeinate 的 -s 断言只在 AC 下有效）、
   已登录京东且未被注销，时区正确（--start 用的是本机时间）。

试跑（会真的调用兑换接口）：launchctl kickstart -k gui/$(id -u)/com.jd.grab
或直接：./deploy/macos/run_grab.sh --test
EOF
