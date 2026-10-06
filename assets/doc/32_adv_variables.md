# 🔀 变量与流程控制全解

蓝图像积木，但**变量是蓝图像编程语言一样强大的秘密武器**。这一节把变量、VarSwitch 多条件分支、Loop 循环、Sequence 顺序执行这几个核心控制流拆透。

---

## 一、变量系统总览

### 变量在哪里存在？
每个**蓝图运行时实例**都有一张独立的字典 `_variables`：
```
{
  "hp":        85,          # int
  "mana":      120.5,       # float
  "hp_text":   "HP: 85",    # str
  "name":      "赫萝",      # str
  "ok":        True,        # bool
  "pos":       (100, 200),  # tuple2
}
```
→ 两个蓝图并行跑时，变量**互不影响**。

### 能存什么类型？
| 类型 | 字面量写法 | 说明 |
|------|-----------|------|
| `int`   | `42`, `0`, `-5` | 整数 |
| `float` | `3.14`, `1.0` | 小数 |
| `str`   | `"赫萝"`, `'狼'` | 字符串 |
| `bool`  | `True`, `False` | 布尔 |
| `tuple2`| `(1280, 720)`   | 坐标对（Mouse/FindImage 传位置） |
| `list`, `dict`, `None` | `[1,2,3]`, `{"a":1}`, `None` | 少用，大多走 JSON 串 |

### 字符串插值：{变量名} 魔法
几乎所有节点的文本/多行文本参数都支持 `{变量名}` 语法：
```
PressKey.text = "今天 {name} 想吃 {food}，一共 {count} 个苹果 🍎"

如果变量 name="赫萝", food="面包", count=3 → 实际写入剪贴板的是：
"今天 赫萝 想吃 面包，一共 3 个苹果 🍎"
```

→ **找不到变量会报错终止蓝图**（不会默默用空字符串代替），因为你大概率写错了名字，需要立刻知道。

### 哪些节点会自动存全局变量？

除了 SetVariable 手动设置外，以下节点执行时会自动写入全局变量：

| 节点 | 写入的变量 | 说明 |
|------|-----------|------|
| **Clipboard** | `{output_var_name}`（默认 `clip_text`） | read 模式读到的文本自动存为指定变量名 |
| **HttpRequest** | `web_` + JSON 响应的每个 key | 勾选 save_json_to_vars 时自动拆 JSON |

> 🐺 这两个节点的变量不需要手动 SetVariable，执行完直接 `{clip_text}`、`{web_token}` 就能用啦~

---

## 二、SetVariable · 设置变量

SetVariable 是存变量的主力节点。在属性面板的 `batch_text` 多行文本框里，每行写 `name = value` 即可：

### ① 字面量赋值
```
hp = 100
count = 0
user = Alice
is_open = true
```

> ⚠ **非常重要**：`name = "苹果"` 里的引号**不会被剥掉**，会作为值的一部分存起来！不想要引号直接写 `name = 苹果`。

### ② 引用其他变量 / 输入端口值
```
# 引用之前设置的变量
final = {hp} + {bonus}

# 引用数据输入端口（num_data_inputs 设了几个就有几个 in_0/in_1…）
cmd_out = {__INPUTS__0}
```

### ③ 表达式计算 `{= 表达式}`
```
# 算术
total = {= {hp} + {bonus}}
double = {= {count} * 2}

# 端口值做计算（Command.stdout 是 str，要先转 int！）
sum = {= int({__INPUTS__0}) + int({__INPUTS__1})}
```

### ④ 注释
```
# 这一行以 # 开头，是注释
// 双斜线也是注释
```

### ⑤ 数据出口：把变量传给「组件」/「结束」节点

`batch_text` 里写了几个变量，节点**右侧就会长出几个同名数据出口**（○），执行时输出刚设置的那个值。

```
SetVariable： a = 123
连线：SetVariable.a  →  组件.in_0
```

被调用的组件蓝图里，这个值从它内部 `Start` 节点的 `param_0`（数据输出端口）取用；
组件要把结果交还给调用方，则反过来连：`SetVariable.xxx → 结束.ret_0`，调用方从 `组件.out_0` 取回。

> ⚠ **SetVariable 必须排在组件 / 结束节点之前执行**（执行流顺序）——数据出口给的是「上游已经执行过的输出」，
> 顺序反了下游只会读到空值 `None`，而且不报错。

> ⚠ **写不对的行不会生成出口**：变量名须以**字母 / 下划线 / 汉字**开头
> （`血量 = 100` ✓、`hp = 100` ✓；`my-var`、`1a`、带空格的 ✗）；整行以 `#` 或 `//` 开头算注释、也不算变量。
> 卡在这个点上时：画布上该行会标 `⚠`、「✓ 检查格式」按钮会指出第几行错、运行日志也会 WARN。

> 详细的 batch_text 语法和端口用法见 [SetVariable 节点详解](node_SetVariable.md)。

---

## 三、VarSwitch · 多条件分支（if / elif / else）

### 本质就是一串 if elif else
```
condition_list = [
  (mode, left, op, right, label),  ← cond_0
  (mode, left, op, right, label),  ← cond_1
  ...
]

按顺序依次评估：
  if   cond_0 为真 → case_0_exec  ▶
  elif cond_1 为真 → case_1_exec  ▶
  elif cond_2 为真 → case_2_exec  ▶
  ...
  全假            → default_exec  ▶（default 没连下游就静默）
```

### 两种评估模式 mode
| mode | 含义 | 要求 |
|------|------|------|
| **var**（变量比较） | 取 `variables[left]` 做运算 | left 是变量名 |
| **literal**（字面量比较） | 直接把 left 字面量值当左操作数 | left 能解析成 int/float/str/bool/tuple2 |

### 支持的运算符 op（按大类分）
| 类别 | 运算符 | 例 |
|------|--------|-----|
| 比较 | `==`, `!=`, `<`, `<=`, `>`, `>=` | hp `>` 50 |
| 包含 | `in`, `not_in` | "苹果" `in` bag_text |
| 类型 | `is_int`, `is_float`, `is_str`, `is_bool`, `is_none`, `is_tuple2` | money `is_int` |
| 字符串 | `starts_with`, `ends_with`, `contains_ci`（大小写不敏感包含） | title `starts_with` "第" |

### Inspector 里怎么加条件？
右侧属性面板「➕ 添加条件」一行一行加：
1. 每一行 = 一个条件分支
2. 选 mode（var 还是 literal）
3. left、op、right 填好
4. label 可选（会显示在节点图标上，短一点如"血低"）
5. 想删点「🗑」；想调顺序点「上移/下移」

添加了条件后，节点图标会自动长出来对应的 **case_N_exec ▶ 输出端口**。

---

## 四、Loop · 两种循环模式

### 模式 A：count（固定次数循环）
```
loop_mode  = count
loop_count = 5
```
```
for i in range(0, 5):
    跑 loop_body ▶ → 等下游跑完
```
Loop 的数据输出端口 `out_iteration`（当前轮次，从 1 开始）和 `out_max`（总次数，无限循环为 -1）可以连到下游节点的数据输入端口使用。

> ⚠ Loop **不会**自动写全局变量。如果想在 Notify 里显示当前轮次，需要把 `out_iteration` 连到 SetVariable 的数据端口，再 `index = {__INPUTS__0}` 存成变量。

### 模式 B：infinite（死循环 + break）
```
loop_mode  = infinite
```
→ 除非某个分支触发 `break_in ▶`（或者点停止按钮手动停），否则永远循环。

常用模式：
```
Loop(infinite)
  loop_body →
    IfImageDetect(找到BOSS?)
      yes → Loop.break_in   ← 找到了跳出循环
      no  → Wait(2s)        ← 没找到继续等
```

### Loop 也有默认输出
- `finished`：count 模式跑完所有次数后触发；infinite 模式被 break 后才触发
→ 在 finished 后面接"循环结束后的收尾工作"。

---

## 五、Sequence · 三种子流分发模式

Sequence 是高级用法，解决"一个触发点我要同时做好几件事，每件事内部节点又不一样多"的场景。

### 模式 1：sequential（顺序串行执行）
```
in_exec → 子流 0 跑完 → 子流 1 跑完 → 子流 2 跑完 → done
```
→ 适合：做完 A 再做 B 再做 C，但 A B C 各自内部又是一串节点，不想每个子流的末尾都去连下一个子流的头。

### 模式 2：parallel_wait（并行等待全部）
```
in_exec ─┬─ 子流 0 开跑 ──┐
         ├─ 子流 1 开跑 ──┤
         └─ 子流 2 开跑 ──┘
              三者全跑完 → done
```
→ 适合：同时打开三个网页、同时点三个互不影响的按钮……必须等三件事全做完再继续。

> 注意：这是"伪并行"，不是真多线程。节点逐个跑，但把子流的执行交错安排了。对于有实际等待的节点（Wait、FindImage 超时等）才真的能省时间。

### 模式 3：parallel_first（赛跑模式，谁先中就谁）
```
in_exec ─┬─ 子流 0 → 找到了 OK 立刻停止其他子流 → done
         ├─ 子流 1 → （被中断）
         └─ 子流 2 → （被中断）
```
→ 适合：三种弹窗同时可能出现，哪一个先冒头就处理哪一个，处理完其他就不用判断了。
→ **第一个触发到自己末端执行端口的子流**赢。

### 怎么添加子流？
Inspector「➕ 添加子流」一行一行加，每个子流对应一个 `in_N ▶` 输出端口，Sequence 内部会按配置的模式调度这些端口触发。

---

## 六、典型组合套路 🍎

### 套路 1：血量监视（循环 + 分支）
```
Start → OpenApp → Loop(infinite)
  loop_body →
    IfImageDetect（或其他手段获取 hp 变量）
    VarSwitch：
      var hp <  30 → PressKey(按吃药快捷键) → Notify("吃药！血量极低")
      var hp <  60 → Notify("血量有点少，注意")
      default     → （啥也不做）
    Wait(0.5s)  ← 每 0.5 秒检查一次，别跑太满
```

### 套路 2：每天签到 5 次（计数循环 + 找图点击）
```
Start → OpenApp →
  Loop(count=5)
    loop_body →
      FindImage("签到按钮", after_action=click)
      Notify("完成第 {index}/{loop_count} 次签到")
      Wait(1s)
    finished →
      Notify("5 次签到全完成啦~") → End
```

### 套路 3：多个弹窗并行处理（parallel_first）
三个可能弹窗之一冒头就关掉，关掉后继续：
```
Sequence(parallel_first)
  in_0 → FindImage("签到弹窗", after_action=click)  → 子流末端 ✅ 赢
  in_1 → FindImage("公告弹窗", after_action=click)  →（如果先触发就是它赢）
  in_2 → FindImage("活动弹窗", after_action=click)  →
done → 处理完任意一个，继续跑下游流程
```
