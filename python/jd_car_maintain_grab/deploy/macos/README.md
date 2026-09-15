# macOS 定时运行（持续接电源）

用 LaunchAgent 每天 09:50 拉起抢购，并用 `caffeinate` 保证「等到 10:00:00 开抢」
这段期间机器不会睡过去。前提：**Mac 持续接电源**（`caffeinate -s` 的断言只在 AC 下
有效）、已在该 Mac 上登录过京东、时区正确。

## 时间线

| 时刻 | 动作 |
|---|---|
| 09:45 | `pmset` 唤醒 Mac（launchd 自己不会唤醒睡眠中的机器） |
| 09:50 | LaunchAgent 启动 `run_grab.sh`：`caffeinate` 持锁 → 启动 Chrome 打开活动页 → 读兑换参数 |
| 10:00:00 | 放库存即开抢：前 2 秒每 100ms 一次，之后每 1000ms 一次 |
| ~10:00:22 | 跑满 `API_ATTEMPTS=40` 次后退出，并关闭 Chrome；`caffeinate` 释放锁 |

## 安装

```bash
cd /path/to/jd_car_maintain_grab
./deploy/macos/install.sh                       # 渲染 plist、加载 LaunchAgent

# 定时唤醒（需要 sudo，install.sh 不会替你执行）：
sudo pmset repeat wakeorpoweron MTWRFSU 09:45:00
pmset -g sched                                  # 确认时间表
```

想改启动时刻：改 `deploy/macos/com.jd.grab.plist` 里的 `StartCalendarInterval`，
再跑一次 `install.sh` 即可。

## 验证

```bash
pmset -g sched                                   # 唤醒时间表
launchctl print gui/$(id -u)/com.jd.grab         # LaunchAgent 状态
pmset -g assertions                              # 运行时应看到 caffeinate 持有 PreventUserIdleSystemSleep
tail -f logs/launchd.out.log                     # launchd 捕获的终端输出
tail -f logs/jd_grab_$(date +%F).log             # 脚本自己的执行日志（含抢购结果）
```

执行日志里可用来判断「没抢到」的原因：`登录态正常` / `检测到未登录` / `登录未完成`
（登录态失效）、`等待到 10:00:00.000 放库存开抢`（时序正常）、每条接口返回的业务码
（`1714001` = 当时不可兑换：未开抢 / 已抢完 / 不在兑换时段）。

## 卸载

```bash
launchctl bootout gui/$(id -u)/com.jd.grab
rm ~/Library/LaunchAgents/com.jd.grab.plist
sudo pmset schedule cancelall
```

## 注意

- **必须装在用户会话**（LaunchAgent，`gui/<uid>`），不能用 LaunchDaemon：脚本要启动
  真实 Chrome 窗口。请保持用户处于登录状态、不要注销。
- **合盖（clamshell）仍会睡**。接电源 + 外接显示器可避免；要彻底禁掉需
  `sudo pmset -a disablesleep 1`（会持续发热，谨慎）。
- **时区/时钟**：`--start` 用的是本机本地时间，不在东八区要换算；开抢那一瞬以毫秒计较
  （前 2 秒每 100ms 一次），要求时钟准确，请开启自动设置时间（NTP）。
- **stdin 不是终端**：若定时执行时登录态已失效，脚本不会卡在等待输入，而是记录
  `无法读取输入（EOFError）`、重试 5 次后以退出码 `1` 结束。
- `launchctl kickstart -k gui/$(id -u)/com.jd.grab` 或 `run_grab.sh --test`
  会**真的调用兑换接口**，不确定时别乱试。
- 仓库根目录的 `.gitattributes` 已声明 `*.sh` / `*.plist` 用 `text eol=lf`，从 Windows 提交
  再在 Mac 上 checkout 也会是 LF。若是**直接拷贝**文件（不走 git）后报
  `bad interpreter: /bin/zsh^M`，说明被转成了 CRLF：
  `sed -i '' $'s/\r$//' deploy/macos/*.sh`。
