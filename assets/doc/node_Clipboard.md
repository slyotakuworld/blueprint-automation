# 📋 Clipboard · 剪贴板

> 管系统剪贴板的小助手~要么读出来存变量还顺带给下游数据线；要么把汝写的（或上游传进来的）内容写进去；也能一键清空剪贴板哟。

---

## 节点外观

- **分类**：🎯 动作
- **配色**：深紫 (#4527a0 / #7e57c2)
- **短名**：`clipboard`

---

## 端口

### 输入
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `in_exec` | ▶ 执行 | ▶ | 上游触发 |
| `in_text` | ○ 数据 | 写入文本 | 写入模式下，若此端口有连线 → **优先用连线值**，空则用参数里的 value |

### 输出
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `out_done` | ▶ 执行 | 完成 | 操作成功（读/写/清空都算）→ 走这里 |
| `err` | ▶ 执行 | 失败 | 操作失败 → 走这里 |
| `out_text` | ○ 数据 | 剪贴板文本 | 仅 read 模式有效：读到的文本（str） |

---

## 参数一览

| 参数名 | 编辑器 | 默认值 | 必填 | 说明 |
|--------|--------|--------|------|------|
| `mode` | 下拉 | `read` | - | 模式：`read`（读取）、`write`（写入）、`clear`（清空） |
| `value` | 多行文本 | `""` | - | 仅 write 模式生效：要写入的内容，支持 {var} 插值；若 in_text 有连线且非空则优先用连线值 |
| `output_var_name` | 文本 | `clip_text` | - | 仅 read 模式：读到的文本**同时存为全局变量** `clip_text`，下游节点可直接 `{clip_text}` 引用 |

---

## 三种模式详解

### 模式 1：read（读取剪贴板）
```
mode = read
output_var_name = clip_text
```
运行时：
- 把当前系统剪贴板里的文本读出来
- 两条路同时给下游：
  1. 绿色数据端口 `out_text` → 可以直接连到下一个节点（比如 SetVariable.in_0）
  2. 自动存成变量 `clip_text` → 下游所有文本参数 `{clip_text}` 都能用

### 模式 2：write（写入剪贴板）
```
mode = write
value = 赫萝给汝准备好的一段文本：{hello_msg}
```
运行时：
- 如果 `in_text` 绿色端口连了上游，且上游值非空 → 优先写上游的值
- 否则写参数里的 `value`（支持变量插值）

### 模式 3：clear（清空剪贴板）
```
mode = clear
```
→ 把系统剪贴板内容清空，常用于登录密码等敏感信息场景。

---

## 典型用法

### 1. 读剪贴板 → 存变量 → 条件判断
```
连线：
  Start → Clipboard(read) → VarSwitch

Clipboard：
  mode = read
  output_var_name = clip_text

VarSwitch cases：
  {clip_text} == "已授权"
```
→ 如果剪贴板里正好是"已授权"三个字，就走 yes 分支。

### 2. 把命令输出写进剪贴板
```
连线：
  Command.stdout → Clipboard.in_text
  Command.out_done → Clipboard.in_exec

Clipboard：
  mode = write
  value = （这个参数没用，因为连线值优先）
```
→ Command 执行完，把输出直接写进剪贴板，用户可以 Ctrl+V 粘贴。

### 3. 生成一段提示并复制
```
SetVariable：
  order_id = 202607080001
  total = 288.00

→ Clipboard(write)
  value = 【订单通知】订单号{order_id}，合计{total}元，谢谢惠顾~
```
→ 点一下按钮，复制一段格式化好的文本。

---

## 常见问题

**Q：剪贴板里有图片，读出来是啥？**
→ 目前 Clipboard 节点**只处理文本**。图片/文件路径格式的剪贴板内容会被忽略（out_text 是空字符串）。

**Q：写中文乱码？**
→ 不会，内部统一用 Unicode，Windows 剪贴板原生支持中文。
