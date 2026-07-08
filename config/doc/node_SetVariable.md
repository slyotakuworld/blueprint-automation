# 📦 SetVariable · 设置变量

> 存变量用的小盒子~每行写 名字=值，还能塞别的变量进去算一算，存好了后面所有节点都能拿来用哟。

---

## 节点外观

- **分类**：🧠 逻辑组件
- **配色**：深灰+粉 (#2D333B / #F778BA)
- **短名**：`set_var`

---

## 端口

### 输入
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `in_exec` | ▶ 执行 | ▶ | 上游触发 |
| `in_0`、`in_1`… | ○ 数据 | #1 输入、#2 输入… | 动态数据输入端口（最多 20 个），通过 `num_data_inputs` 调整数量 |

### 输出
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `out_exec` | ▶ 执行 | ▶ | 设置完所有变量后触发下游 |

---

## 参数一览

| 参数名 | 编辑器 | 默认值 | 必填 | 说明 |
|--------|--------|--------|------|------|
| `num_data_inputs` | 整数 | `0` | - | 数据输入端口数（0 ~ 20）；改了以后左侧会出现对应数量的绿色端口 |
| `batch_text` | 多行文本 | `""` | - | 批量设置区，每行 `name = value`，支持变量插值和表达式 |

---

## 批量设置语法

### ① 字面量赋值
```
# 数字
hp = 100
count = 0

# 字符串（加不加引号都行，加了引号会连同引号一起存进去！）
user = Alice
hero = "贤狼赫萝"    ← ⚠ 值会包括引号："贤狼赫萝"，不是 贤狼赫萝

# 布尔
is_open = true
flag = false
```

> ⚠ **非常重要**：SetVariable 里 `name = "苹果"`，引号**不会被剥掉**，会作为值的一部分存起来！如果不想要引号，直接写 `name = 苹果`。

### ② 引用其他变量 / 输入端口值
```
# 先连一个 Command.stdout 到 in_0 端口，再：
cmd_out = {__INPUTS__0}      ← 第1个数据端口的值
py_ret  = {__INPUTS__1}      ← 第2个数据端口的值

# 引用其他 SetVariable 节点之前设置的变量
final = {hp} + {bonus}
```

### ③ 表达式计算 `{= 表达式}`
```
# 算术
total = {= {hp} + {bonus}}
double = {= {count} * 2}

# 文本拼接
full_name = {= "{first}_{last}"}

# 端口值做计算（Command.stdout 是 str，要先转 int！）
sum = {= int({__INPUTS__0}) + int({__INPUTS__1})}
```

### ④ 注释
```
# 这一行以 # 开头，是注释
// 双斜线也是注释
```

---

## 临时输入端口变量说明

当 `num_data_inputs = N`（比如 3）时，SetVariable 左侧多出 `in_0`、`in_1`、`in_2` 3 个绿色端口。

在 batch_text 里就这一种写法引用它们：
```
__INPUTS__     = 完整数组，list 类型
__INPUTS__[0]  = 第1个端口值
__INPUTS__0    = 第1个端口值（简写，推荐）
__INPUTS__1    = 第2个端口值
…
__INPUTS__19   = 最多第20个
```

---

## 典型用法

### 1. 蓝图开头定义一堆常量
```
SetVariable batch_text：
  wait_short = 0.5
  wait_long  = 3.0
  game_title = 原神
  threshold  = 0.92
```
→ 下游所有 Wait 节点秒数参数写 `{wait_short}`，Threshold 写 `{threshold}`，一处改处处生效。

### 2. Command 输出存变量
```
连线：
  Command.stdout (绿色) → SetVariable.in_0

SetVariable：
  num_data_inputs = 1
  batch_text：
    cmd_out = {__INPUTS__0}
```
→ 下游 VarSwitch 就能写 `{cmd_out} == "ok"` 做判断了。

---

## 常见问题

**Q：变量保存后可以改吗？**
→ 可以。后执行的 SetVariable 如果用了同一个变量名，新值会覆盖旧值。

**Q：其他节点可以读取变量吗？**
→ 所有节点的所有文本类参数都支持 `{变量名}` 插值。

**Q：变量类型自动推断吗？**
→ 目前做基础推断：`123`→int、`3.14`→float、`true/false`→bool，其他是字符串。需要精确类型时用 `{= int(xxx)}` 手动转。
