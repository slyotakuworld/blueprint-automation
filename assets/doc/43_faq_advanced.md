# 📚 高级 FAQ

> 深入一点的问题：多个蓝图并行、扩展自定义节点、与现有系统集成……等。

---

## 一、多蓝图并行 / 蓝图调用蓝图

### Q1：能不能同时跑两个蓝图？

**A**：可以，但要注意「键鼠操作是全局资源」这一点：
- 蓝图 A 在点窗口 A，同时蓝图 B 也在点窗口 B → 鼠标会被抢着用，两个都点歪
- **安全的并行方式**：A 蓝图负责「键鼠/找图」，B 蓝图负责「HttpRequest / Notify / 变量计算」，互不抢鼠标

启动方式：
咱建议直接多开编辑器实例——入口是 `python src/blueprint_app.py`，跑起来后「窗口」→ 新建蓝图编辑器窗口，分别打开 A、B 两张蓝图，各自点 ▶ 运行就行 🐺。

### Q2：蓝图 A 里能不能调用蓝图 B 像调用子函数一样？

**A**：可以，用「组件(Component)」节点。两步：

1. 把被调用的那张蓝图设为**组件蓝图**：资源树里右键蓝图 → 🧩 设为组件蓝图（放在哪个目录都行）。
2. 在调用方拖一个「组件」节点，选中那张组件蓝图即可。

传参与取值：
- 调用方 `in_0 / in_1…`（数据输入端口）把值传给组件蓝图 `Start` 的入参端口；
- 组件蓝图 `结束` 节点的返回值从 `ret_0 / ret_1…` 回到调用方 `out_0 / out_1…`；
- 两边的端口数量要在运行前对齐，不一致时会有提示（数量对不上会静默错位）；
- 变量仍然是**全局共享**的，不走端口也能读写同一份变量。

> 如果只想要普通 N 个输入/输出的自定义动作，也可以写一个 Node 子类（见下一个问题）做封装。

---

## 二、自定义扩展节点

### Q3：我想加一个「发送微信消息」这样的功能节点，怎么写？

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

## 三、与现有系统集成

### Q4：能用 HTTP 接口远程触发一张蓝图运行吗？

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

### Q5：节点的 out_pos 能不能写进数据库？

**A**：HttpRequest 节点直接调你自己的后端接口即可：
```
FindImage("怪物头像")
  found 分支 →
    HttpRequest：
      method = POST
      url    = "https://your-api/game/punch"
      body   = '{"pos_x": {X}, "pos_y": {Y}, "found": true}'
```
→ 把 FindImage 输出的位置用 SetVariable 写成 X/Y 两个变量，再在 HttpRequest 的 JSON 里 {X} {Y} 插值就发出去了。

---

## 四、其他

### Q6：想捐点苹果给作者（划掉），怎么反馈 Bug？

**A**：
- 附上：错误截图 + 蓝图 .json 文件（可以脱敏）+ 最近一次运行日志
- 去对应的 Issue 反馈渠道按模板填写。能附一张截图胜过长篇大论。

### Q7：蓝图支持多显示器吗？

**A**：支持。FindImage「找图范围」= 全屏时，截图工具默认截主显示器。
如果目标窗口在第二个显示器：
1. 目标窗口是按窗口句柄定位的，跟它在哪个显示器没关系
2. 把目标窗口拖回主显示器再跑（最简单）
3. 或者把「找图范围」「坐标基准」都选「目标窗口」，坐标相对窗口自动换算，跨屏也没问题
