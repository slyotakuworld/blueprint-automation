# 💻 Command · 执行命令

> 跑Windows命令或者外部程序用哒~命令里能塞变量，三种shell模式随便挑；不想等程序关掉就往下跑可以关了捕获；命令报错退出码非零也可以当失败处理；标准输出、错误、退出码都会捡回来给下游用哟。

---

## 节点外观

- **分类**：🚀 应用控制
- **配色**：橙红 (#bf360c / #f4511e)
- **短名**：`cmd`

---

## 端口

### 输入
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `in_exec` | ▶ 执行 | ▶ | 上游触发 |

### 输出
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `out_done` | ▶ 执行 | 完成 | 命令执行完（或不捕获立即返回）→ 走这里 |
| `err` | ▶ 执行 | 失败/超时 | 超时 / 执行失败 / 非零退出码（勾了 fail_on_nonzero）→ 走这里 |
| `stdout` | ○ 数据 | 标准输出 | 命令的标准输出文本（str） |
| `stderr` | ○ 数据 | 标准错误 | 命令的标准错误输出文本（str） |
| `exit_code` | ○ 数据 | 退出码 | 进程退出码，0=正常（int） |

---

## 参数一览

| 参数名 | 编辑器 | 默认值 | 必填 | 说明 |
|--------|--------|--------|------|------|
| `command` | 多行文本 | `echo hello {name}` | - | 要执行的命令，支持 `{var}` 变量插值 |
| `working_dir` | 📁 路径 | `""` | - | 工作目录；留空=沿用主程序当前目录；支持 `{变量}` 插值和特殊占位符 `{blueprint_dir}`=当前蓝图所在文件夹 |
| `timeout_ms` | 整数 | `10000` | - | 超时毫秒；0=永不超时（慎用，可能卡死蓝图） |
| `capture_streams` | ✅ 复选 | `true` | - | 捕获输出。关掉后不会等程序结束，立刻 out_done（适合 GUI 程序：notepad.exe、游戏） |
| `shell_mode` | 下拉 | `cmd_c` | - | shell 模式：`cmd_c`（CMD /c）、`powershell`（PowerShell -NoProfile -Command）、`no_shell`（直接 CreateProcess） |
| `fail_on_nonzero` | ✅ 复选 | `false` | - | 勾了：exit_code!=0 → 走 err；不勾（默认）：不管退出码都走 out_done，下游自己用 VarSwitch 判断 exit_code |

---

## 三种 Shell 模式区别

| 模式 | 说明 | 典型用途 |
|------|------|----------|
| `cmd_c` | 等价于 `cmd.exe /c "命令"` | dir、echo、bat、管道重定向、内部命令（用得最多，默认） |
| `powershell` | 用 PowerShell 执行 | 复杂脚本、操作 .NET 对象、管道传对象 |
| `no_shell` | 直接 CreateProcess 不经过 shell | 安全、快；但不能用管道/重定向/内部命令 |

---

## 典型用法

### 1. 列 C 盘根目录
```
command       = dir C:\
working_dir   = （留空）
timeout_ms    = 5000
capture_streams = ✅
```
→ `stdout` 输出就是 dir 命令的文本结果，后续节点可以用 `{exit_code}` 看是不是 0。

### 2. 打开记事本（不等它关闭）
```
command         = notepad.exe
capture_streams = ❌ 关闭捕获
```
→ 立刻 out_done，蓝图继续往下走，记事本窗口保留。

### 3. 跑 Python 脚本并拿到输出
```
command   = python D:\scripts\calc.py {arg1} {arg2}
timeout_ms = 30000
shell_mode = cmd_c
fail_on_nonzero = ✅
```
→ `stdout` 就是脚本 print 的内容；脚本报错（非零）→ 走 err 分支。

### 4. 把 stdout 存变量（推荐写法）
Command → 数据线连到 SetVariable 的 in_0 端口：
- SetVariable：`数据输入端口数 = 1`
- 批量设置：`py_out = {__INPUTS__0}`

然后 VarSwitch 写：
```
__INPUTS__0 == "ok"   ← stdout 第1个端口连到 VarSwitch
```

> ⚠ **重要**：Command 节点的 stdout 默认是 **字符串 str 类型**，如果下游要做数值比较，**必须手动转类型**！
> 正确示例：`{= int({__INPUTS__0}) + 1}`
> 错误示例：`{__INPUTS__0} + 1`（字符串拼接，不是加法）

---

## 常见问题

**Q：命令里有引号、特殊字符怎么办？**
→ cmd_c 模式下用 CMD 的标准转义，或者写进 .bat 文件再调用 .bat。

**Q：路径里有空格？**
→ 把路径用一对双引号括起来：`"C:\Program Files\xxx.exe"`。

**Q：中文乱码？**
→ CMD 默认是 GBK 编码，把命令改成：`chcp 65001 > nul && dir`（先切 UTF-8 再执行）。
