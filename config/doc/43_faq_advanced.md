# 📚 高级 FAQ

> 深入一点的问题：和 LLM 助手怎么配合、多个蓝图并行、扩展自定义节点……等。

---

## 一、和 LLM 助手配合相关

### Q1：「自由聊天」Tab 旁边的「蓝图生成」Tab 是做什么的？

**A**：用自然语言描述你想做的自动化 → 让 LLM 直接帮你生成一张蓝图节点布局（节点列表 + 坐标 + 连线 + 参数），然后一键保存为蓝图文件。

举个例子，你说：
```
帮我生成一个每天早上打开游戏签到的蓝图：
1. 打开 D:\Games\xxx.exe
2. 绑游戏窗口
3. 等加载静止 10 秒
4. 点"签到"按钮（我等下自己把模板图补上）
5. 签到 3 次
6. 通知"签到完成"
```

助手会按这个描述，输出 OpenApp → BindWindow → StillnessDetect → FindImage(签到按钮) → Loop(count=3) → Notify 的完整蓝图。

> ⚠ 注意：生成的蓝图里，FindImage 的模板图路径、按钮坐标这些**必须你自己补**（LLM 不可能知道你电脑上截图具体长啥样）。收到后导入编辑器，填好这些就能直接跑。

### Q2：为什么「代码助手」Tab 会自动解析错误堆栈？

**A**：点了「自动分析代码错误堆栈」按钮后，后端会：
1. 把最近一次运行错误的堆栈（traceback）截出来
2. 拼到给 LLM 的 prompt 里：`"请分析这段 Python 错误并给修复方案"`
3. LLM 返回的结果用 Markdown 格式显示在对话里

→ 相当于"请个 AI 同事帮你看报错"。

### Q3：历史消息会占显存吗？多久会自动删？

**A**：
- 历史消息保存在本地磁盘（不是内存/显存里），就在应用自己的 data 目录下：
  ```
  data/llm_history/{会话ID}.jsonl
  ```
- 每一条记录都有 `timestamp / role / content`。
- 不会自动删除，想清理直接去「对话历史」Tab 选中会话点「删除」。

### Q4：图像分析一次大概多少 token？

**A**：视觉模型处理一张图 ≈ 200~2000 token 的视觉部分（取决于图像大小和模型）+ prompt / reply 的文本部分。
- 大多数识图场景（「这张图里的按钮在哪里？」），1000~2000 token 量级
- 模型自己会做图片压缩（最长边 2000px 以下）

---

## 二、多蓝图并行 / 蓝图调用蓝图

### Q5：能不能同时跑两个蓝图？

**A**：可以，但要注意「键鼠操作是全局资源」这一点：
- 蓝图 A 在点窗口 A，同时蓝图 B 也在点窗口 B → 鼠标会被抢着用，两个都点歪
- **安全的并行方式**：A 蓝图负责「键鼠/找图」，B 蓝图负责「HttpRequest / Notify / 变量计算」，互不抢鼠标

启动方式：
咱建议直接多开编辑器实例——入口是 `python src/blueprint_app.py`，跑起来后「窗口」→ 新建蓝图编辑器窗口，分别打开 A、B 两张蓝图，各自点 ▶ 运行就行 🐺。

### Q6：蓝图 A 里能不能调用蓝图 B 像调用子函数一样？

**A**：目前版本还没有「Blueprint → Blueprint 子流程节点」。变通方案：
- 把公共部分（比如"登录游戏"的那一段）复制粘贴到每张蓝图里
- 或者写一个 Node 子类（见下一个问题）做封装

> 🍎 后续版本会增加「BlueprintRef 节点」—— 拖一个节点指向另一个 .json 文件，跑的时候就像子函数一样调用。

---

## 三、自定义扩展节点

### Q7：我想加一个「发送微信消息」这样的功能节点，怎么写？

**A**：三步：

#### Step 1：在 `node_library.py` 里加一个节点定义
```python
# 在 _NODE_DEFS 里加一个 dict：
{
    "type": "SendWechat",
    "name": "发送微信",
    "category": "🤝 外部集成",    ← 会自动分到这一组
    "color": ("#07C160", "#51CF66"),
    "short_name": "wechat",
    "params": [
        ParamDef("bot_key",  EDITOR_STRING,   "机器人KEY",  "",    "String"),
        ParamDef("msg",      EDITOR_MULTI,    "消息内容",   "",    "String"),
    ],
    "ports_in":  [("in_exec", TYPE_EXEC,    "▶")],
    "ports_out": [("ok",      TYPE_EXEC,    "成功"),
                  ("fail",    TYPE_EXEC,    "失败")],
    "handler": "send_wechat",   ← Step 2 写这个 handler
    ...
}
```

#### Step 2：在 `executor.py` 里注册同名 handler
```python
# 在 handler 注册表加：
def handler_SendWechat(node, ctx):
    bot_key = node.params.get("bot_key", "")
    msg     = resolve_vars_in_str(node.params.get("msg", ""), ctx.variables)
    ok = call_wechat_webhook(bot_key, msg)
    if ok:    ctx.emit_exec("ok")
    else:     ctx.emit_exec("fail")
```

#### Step 3：重启蓝图编辑器
- 左侧节点库自动会多出一个「发送微信」节点
- 在「🤝 外部集成」分类下就能拖出来用

> 完整的节点定义、端口类型、编辑器类型说明看 [节点参数编辑器约定](file:///g:/work/BlueprintAutomation/config/doc/30_adv_node_params.md) 那一节。

---

## 四、与现有系统集成

### Q8：能用 HTTP 接口远程触发一张蓝图运行吗？

**A**：目前蓝图软件本身不带 HTTP 服务。最简单的做法：
1. 安装一个 Python HTTP 服务（如 FastAPI）：
   ```python
   from fastapi import FastAPI
   import subprocess
   app = FastAPI()

   @app.post("/run/{name}")
   def run_bp(name: str):
       subprocess.Popen([
           "python", "src/blueprint_app.py"
       ])
       return {"ok": True}
   ```
2. 外部系统 POST `http://localhost:8000/run/日常任务` → 这台电脑就会跑那张蓝图。

> 更稳的方式：搭配 Celery / MQ 队列。蓝图本身是命令行模式的，任何能启动进程的调度系统都可以对接。

### Q9：节点的 out_pos / out_found 能不能写进数据库？

**A**：HttpRequest 节点直接调你自己的后端接口即可：
```
FindImage("怪物头像")
  out_found=True →
    HttpRequest：
      method = POST
      url    = "https://your-api/game/punch"
      body   = '{"pos_x": {X}, "pos_y": {Y}, "found": true}'
```
→ 把 FindImage 输出的位置用 SetVariable 写成 X/Y 两个变量，再在 HttpRequest 的 JSON 里 {X} {Y} 插值就发出去了。

---

## 五、安全性 / 配置相关

### Q10：LLM API Key 明文保存在 config.ini 里安全吗？

**A**：
- Key 只保存在**你自己的电脑**上的 `config/llm_config.json`，不会上传到任何第三方服务器（除了你配置的 API Base 地址本身）
- 如果对安全要求高：
  - 用环境变量：`BLUEPRINT_LLM_API_KEY` 程序启动时优先读这个
  - 或者给 config 目录设置 NTFS 权限，只给当前用户读

### Q11：日志里会不会泄露我的 API Key？

**A**：不会。日志里所有 Authorization 头、api_key 参数都会在写日志前被过滤成 `***`。
→ 看源码：`llm_session.py` 的 `_safe_log` 方法做了统一正则过滤。

---

## 六、其他

### Q12：想捐点苹果给作者（划掉），怎么反馈 Bug？

**A**：
- 附上：错误截图 + 蓝图 .json 文件（可以脱敏）+ 最近一次运行日志
- 去对应的 Issue 反馈渠道按模板填写。能附一张截图胜过长篇大论。

### Q13：蓝图支持多显示器吗？

**A**：支持。FindImage search_mode = absolute 时，截图工具默认截主显示器。
如果目标窗口在第二个显示器：
1. BindWindow 绑的是窗口句柄，跟哪个显示器没关系
2. 把目标窗口拖回主显示器再跑（最简单）
3. 或者用相对模式（relative）自动处理
