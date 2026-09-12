# 🌐 HttpRequest · 网络请求

> 调网络接口用哒~GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS 七种方法随便选，URL、请求头、请求体里都能塞变量，响应字典还能自动拆成 web_ 开头的变量直接用。

---

## 节点外观

- **分类**：🚀 应用控制
- **配色**：深蓝靛 (#1a237e / #5c6bc0)
- **短名**：`http_request`

---

## 端口

### 输入
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `in_exec` | ▶ 执行 | ▶ | 上游触发 |

### 输出
| 名称 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `out_done` | ▶ 执行 | 完成 | 请求成功返回（HTTP 状态码不管 200 还是 404 只要有响应就走这里） |
| `err` | ▶ 执行 | 失败/超时 | 网络不通 / DNS 失败 / 超时 / SSL 错误 → 走这里 |
| `response_text` | ○ 数据 | 响应原文 | str，HTTP 响应 body 的原始文本 |
| `response_json` | ○ 数据 | 响应JSON(字典) | dict，如果响应不是合法 JSON → 空字典 `{}` |
| `status_code` | ○ 数据 | HTTP状态码 | int，比如 200 / 404 / 500 |

---

## 参数一览

| 参数名 | 编辑器 | 默认值 | 必填 | 说明 |
|--------|--------|--------|------|------|
| `method` | 下拉 | `GET` | - | HTTP 方法：GET / POST / PUT / DELETE / PATCH / HEAD / OPTIONS |
| `url` | 文本 | `https://example.com/api?name={name}` | ✅ | 接口 URL，支持 `{var}` 变量插值 |
| `headers_json` | 多行文本 | `{ "Authorization": "Bearer {token}", "User-Agent": "BA/1.0" }` | - | JSON 格式请求头；支持 {var}；空=不自定义头 |
| `body_text` | 多行文本 | `{"key": "{var}", "num": 123}` | - | 请求体；支持 {var}；GET/HEAD 自动忽略；空=不发请求体 |
| `content_type` | 文本 | `""` | - | Content-Type；空=有请求体且未自定义时自动填 `application/json; charset=utf-8` |
| `timeout_ms` | 整数 | `10000` | - | 超时毫秒（500 ~ 300000） |
| `save_json_to_vars` | ✅ 复选 | `true` | - | 勾了：响应 JSON 若是字典，每个 key 自动存到 `ctx.variables["web_"+key]`，后续节点写 `{web_data}` 就能用 |

---

## 典型用法

### 1. GET 一个公开接口
```
method = GET
url    = https://httpbin.org/get?user={user}
timeout_ms = 8000
```
→ `response_text` 里就是返回的 JSON 文本，`status_code` 一般是 200。

### 2. POST JSON
```
method    = POST
url       = https://api.example.com/v1/login
headers_json = {
  "Content-Type": "application/json",
  "X-App": "BA"
}
body_text = {
  "username": "{user}",
  "password": "{pwd}"
}
save_json_to_vars = ✅
```
→ 如果返回 `{"token": "abc123", "uid": 9527}`：
- 自动生成 `web_token = "abc123"` 和 `web_uid = 9527` 两个变量
- 下游 VarSwitch 可以直接写 `{web_uid} >= 1`

### 3. 带 Authorization 头（先 SetVariable）
SetVariable：
```
token = sk-xxxxxxxxx
```
HttpRequest：
```
headers_json = {
  "Authorization": "Bearer {token}"
}
```

---

## 常见问题

**Q：返回的是 JSON，但 response_json 是空字典？**
→ 要么 body 不是合法 JSON（比如 HTML 错误页），要么 HTTP 500 / 404，看一下 response_text 就知道了。

**Q：怎么判断是 404 还是 200？**
→ 不管 HTTP 状态码（200/404/500 都一样）都走 out_done，err 仅在网络错误（DNS 失败、超时、SSL 错误等）时触发。接一个 VarSwitch，条件写 `{status_code} == 200` 判断是不是成功。

**Q：HTTPS 自签证书报错？**
→ 程序按系统默认规则校验 SSL 证书，自签或不受信任的证书会导致请求失败（走 err 分支）。需要访问此类接口时，请让服务端使用受信任的证书。
