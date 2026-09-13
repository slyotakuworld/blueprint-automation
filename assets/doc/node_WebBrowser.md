# 🌐 WebBrowser · 打开网页

> 帮汝开浏览器访问指定网址，默认浏览器或者Chrome/Edge/Firefox随便汝选。

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
| 输出 | ▶ 执行 | `err` | 失败 | 所有打开方式均失败（指定浏览器未找到且系统默认浏览器也失败）→ 走这里 |

---

## 参数一览

| 参数名 | 编辑器 | 默认值 | 必填 | 说明 |
|--------|--------|--------|------|------|
| `url` | 文本 | `https://www.baidu.com/s?wd={keyword}` | ✅ | 要访问的网址，支持 `{var}` 变量插值 |
| `browser` | 下拉 | `"默认"` | - | 浏览器：默认 / Chrome / Edge / Firefox |

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

### 3. 指定 Chrome
```
url     = https://www.youtube.com
browser = Chrome
```

---

## 常见问题

**Q：「默认」是用哪个浏览器？**
→ 调 Windows API `ShellExecute`，用系统默认的 .html/URL 关联程序，就是你平时双击 HTML 文件打开的那个。

**Q：选了 Chrome，但电脑里没装？**
 不会走 err——代码会自动 fallback 到系统默认浏览器打开。只有所有方式都失败才走 err。

**Q：怎么开无痕模式？**
→ 用 Command 节点手动传参数，例如：
`path/to/chrome.exe --incognito https://example.com`。
WebBrowser 节点只做最简单的「打开 URL」，复杂启动参数交给 Command~
