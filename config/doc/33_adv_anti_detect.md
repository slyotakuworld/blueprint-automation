# 🐺 拟人化操作与反检测

蓝图自动化工具有时会运行在"不希望被一眼看出是机器人"的场景（比如游戏日常任务挂机）。这一节整理几个让操作更像真人的小技巧，以及节点里对应的参数。

> 🍎 提示：工具本身不会绕过任何游戏的反作弊驱动程序（如 EAC、BattlEye），它们是内核级的，请自行评估在某个具体软件里是否允许自动化。这里的技巧只针对「通过图像/声音识别做界面级 RPA」的行为检测。

---

## 一、操作时间的"随机抖动"是核心

真人的操作时间**不会每次都精确到同一个数值**。例如你自己点一个按钮，每次按下去按住的时长大约是 40~90 ms 随机，不是永远 50 ms。

蓝图里几乎所有和"时长/间隔"有关的参数都给了 min/max 两个值（或区间，或固定值+区间）：

| 节点 | 参数对 | 效果 |
|------|--------|------|
| Mouse | `hold_ms_min` ~ `hold_ms_max` | 按住按钮时长在区间里随机 |
| Mouse | `post_delay_min_ms` ~ `post_delay_max_ms` | 操作完再随机小睡一会 |
| Wait | `min_seconds` ~ `max_seconds`（random 模式） | 两次动作之间的延迟每次都不一样 |
| PressKey(key) | `hold_ms` + `interval_ms` | 按键保持时长和间隔均为固定值（无随机性，需随机化用 Mouse 节点或 Wait 节点 random 模式） |

### ✅ 推荐默认值（日常挂机不用改）
```
Mouse.hold_ms_min     = 30
Mouse.hold_ms_max     = 80
Mouse.post_delay_min  = 30
Mouse.post_delay_max  = 80
Wait （random）       = 0.2 ~ 0.6 秒（简单操作间）
                      = 1.0 ~ 3.0 秒（页面加载类）
```

### ❌ 反例（机器人味十足）
```
Wait(seconds=1.0, fixed)  ← 永远精确 1 秒，间隔毫无变化
Mouse.hold_min=hold_max=50 ← 每次都精准 50 ms
```

---

## 二、鼠标移动的"平滑曲线"

### Mouse action = drag 拖拽
拖拽不是瞬间位移：
```
drag_duration_ms = 300   ← 默认 300ms
```
→ 内部按平滑贝塞尔曲线移动鼠标，起点→ 控制点 → 终点的速度是先加速后减速（鼠标加速度曲线）。

拖拽持续时间越长，看起来越像人手在拖。调参建议：
| 场景 | drag_duration_ms |
|------|------------------|
| 物品栏 2 格短距离 | 150 ~ 200 |
| 跨越半个屏幕 | 350 ~ 600 |
| 极短距离（精细瞄准） | 80 ~ 120 |

### action = click 之前要不要提前移动？
→ 如果是从 FindImage 连过来，FindImage 里的 `after_action = click` 内部已经带了一次 move→press→release 流程，这个 move 的速度是默认的（不会瞬间飞过去）。

---

## 三、找图的"偶尔失败一下"

这一点**不建议做**——让蓝图本身不稳定是自找麻烦。真需要的话，可以在 VarSwitch 里随机触发：

```
SetVariable(compute, ok = "rand() < 0.98")  → 98% 的时候才点
VarSwitch(ok True → Mouse 点按钮；False → 啥也不做等一轮)
```
→ 这样每 50 次会有 1 次"假装没看到按钮下次再点"。**慎用！！！让流程不稳定的代价非常大。**

---

## 四、活动时间与"人类作息"

Loop infinite 24 小时永不停 → 太假。

推荐做法：
- 用 Windows 计划任务 / 桌面小工具**只在固定时间启动**蓝图（比如早上 9:00 开始跑日常）
- 或者蓝图开头用 `PressKey(start app)` 启动目标程序，晚上 `End` 节点收工

→ 如果一定要在 Loop 里控制"每天只跑一次"，自己加一个变量：
```
SetVariable(set, today = strftime 今天日期)
VarSwitch(today 存的 != 已完成日期 → 跑日常；默认 → 蓝图睡 10 分钟再检查)
```

---

## 五、组合拳：完整「游戏日常」模板示例

```
Start
 ├─ 1. OpenApp（游戏路径，等待 15 秒）
 ├─ 2. BindWindow（绑游戏窗口，resolution 1280×720 方便坐标）
 ├─ 3. Loop(count=10)   ← 假设 10 个每日任务
 │     loop_body →
 │       FindImage(接取任务按钮, click)
 │         ├─ 阈值 0.9，hybrid 模式
 │         ├─ scroll_dy=-200（找不到就往下滚列表）
 │       Wait(random 0.3~0.8)
 │       IfImageDetect（任务完成标记，OR 逻辑短路）
 │         yes → 继续；no → FindImage(自动寻路按钮, click)
 │       Wait(random 8.0~14.0)  ← 等任务跑完（拟人）
 ├─ 4. Notify("10 个日常全完成啦~ 苹果给咱当奖励 🍎")
 └─ 5. End
```

时间上每次跑都有 ± 几秒的随机抖动，不会精确到秒。

---

## 六、调参速查表 🍎

| 目标 | 操作 |
|------|------|
| 更像真人 | Wait 全用 random 模式；Mouse hold/post_delay 设 min≠max |
| 更快 | Wait 设 fixed 0.1s；drag_duration_ms 调小；Mouse hold_min/max 调小 |
| 更稳定（找图） | FindImage 阈值 0.9+；模板图截紧凑；search_mode 绑窗口减少误判 |
| 更稳（点击） | FindImage after_action=click 比单独 FindImage→Mouse 更不容易坐标错位 |
