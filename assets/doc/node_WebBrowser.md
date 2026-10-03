# 🌐 WebBrowser · 打开网页

> 帮汝开浏览器访问指定网址，默认浏览器或者Chrome/Edge/Firefox随便汝选，绿色版也能直接指路径。定了哪家就必须用哪家——**找不到直接走「失败」，绝不偷偷换成默认浏览器**哟。

---

## 节点外观

- **分类**：🚀 应用控制
- **配色**：蓝 (#01579b / #29b6f6)
- **短名**：`web_browser`

---

## 端口

| 侧 | 类型 | 名称 | 标签 | 说明 |
|----|------|------|------|------|
| 输入 | ▶ 执行 | `in_exec` | ▶ | 上游触发 |
| 输出 | ▶ 执行 | `out_done` | 完成 | 浏览器启动命令下发成功 → 走这里 |
| 输出 | ▶ 执行 | `err` | 失败 | URL 为空 / 指定浏览器没找到可执行文件 / 完整路径不存在 / 启动浏览器失败 → 走这里（严格模式，不兜底） |

---

## 参数一览

| 参数名 | 编辑器 | 默认值 | 必填 | 说明 |
|--------|--------|--------|------|------|
| `url` | 文本 | `https://www.baidu.com/s?wd={keyword}` | ✅ | 要访问的网址，支持 `{var}` 变量插值 |
| `browser` | 下拉 | `"默认"` | - | 浏览器：默认 / Chrome / Edge / Firefox。**指定了就必须用它，找不到直接走「失败」** |
| `browser_path` | 路径 | `""` | - | 浏览器完整路径。留空=按 `browser` 自动查找；**填了就只用这个 exe，文件不存在直接走「失败」**。绿色版 / 装在 D 盘等非标准位置的浏览器用这个 |
| `wait_sec_after_open` | 浮点 | `5.0` | - | 打开后等待秒数（0~120）：启动命令下发后，本节点再等这么久才走下一节点。填 `0` = 不等 |

---

## 典型用法

### 1. 用默认浏览器打开百度
```
url     = https://www.baidu.com
browser = 默认
```

### 2. 用变量拼接搜索关键词
先 SetVariable：
```
keyword = 赫萝 蓝图自动化
```
然后 WebBrowser：
```
url = https://www.baidu.com/s?wd={keyword}
```
→ 自动打开百度搜索结果页。

### 3. 指定 Chrome（并要求等页面起一会儿）
```
url                 = https://www.youtube.com
browser             = Chrome
wait_sec_after_open = 2.0
```
→ 用 Chrome 打开；命令下发后本节点再等 2 秒才走下一节点。
（`browser` 填了 Chrome 就必须真的用 Chrome——没装 Chrome 会走「失败」，不会拿默认浏览器顶替。）

### 4. 绿色版 / 非标准路径的浏览器
```
url          = https://www.youtube.com
browser      = Chrome          （仅用于日志显示名字，可留默认）
browser_path = D:\绿色浏览器\chrome.exe
```
→ 直接用这个 exe 启动，不去标准路径清单里找。路径写错会走「失败」，不会拿默认浏览器顶替。

---

## 常见问题

**Q：「默认」是用哪个浏览器？**
→ 调 Windows API `ShellExecute`，用系统默认的 .html/URL 关联程序，就是你平时双击 HTML 文件打开的那个。

**Q：选了 Chrome，但电脑里没装会怎样？**
→ **直接走 `err`（失败）出口，不会改用默认浏览器顶替。**
这是刻意设计的「严格模式」：有些操作是绑定浏览器的，静默兜底会把「本该失败」变成「悄悄做错」，事后很难查。
失败时日志会写明「未找到 Chrome 的可执行文件」。想用默认浏览器，把 `browser` 改成 `默认` 就行。

**Q：它去哪找浏览器？不在清单里怎么办？**
→ 按下面顺序找可执行文件，命中第一个就用：
- **Chrome**：`C:\Program Files\Google\Chrome\Application\chrome.exe`、`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`、`%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`、PATH 里的 `google-chrome` / `chrome` / `chromium`
- **Edge**：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`、`C:\Program Files\Microsoft\Edge\Application\msedge.exe`、`%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe`、PATH 里的 `msedge` / `edge`
- **Firefox**：`C:\Program Files\Mozilla Firefox\firefox.exe`、`C:\Program Files (x86)\Mozilla Firefox\firefox.exe`、PATH 里的 `firefox` / `mozilla`

绿色版、装在 D 盘等非标准位置的浏览器不在清单里，会报「未找到」。这种情况把 `browser_path` 填成该 exe 的完整路径即可（例：`D:\绿色浏览器\chrome.exe`），节点就用它启动，不再去清单里找。
（从资源管理器「复制为路径」粘贴过来带的引号会被自动去掉，不用手动清。）

**Q：打开网页后马上就下一个节点了，页面还没加载完怎么办？**
→ 用本节点的 `wait_sec_after_open`（打开后等待，默认 5 秒）。
浏览器启动是异步的：节点只负责把启动命令发出去，**不会等页面加载完成**。
需要更精细的控制（随机延时、从上游变量传秒数）就在后面再接一个「等待」节点。

**Q：怎么开无痕模式？**
→ 用 Command 节点手动传参数，例如：
`path/to/chrome.exe --incognito https://example.com`。
WebBrowser 节点只做最简单的「打开 URL」，复杂启动参数交给 Command~
