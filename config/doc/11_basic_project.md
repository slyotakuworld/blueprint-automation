# 📁 项目管理

## 新建蓝图

1. 顶栏「工程 ▾」下拉 → 新建蓝图
2. 弹出输入框 —— 输入蓝图名称，文件直接创建在当前工程目录下（扩展名 .bp.json）
3. 画布上自动出现绿色「Start」节点，可以直接开始拖节点啦

> 💡 为什么一新建就写盘？
> 因为赫萝想给你一种「蓝图正在被创建」的踏实感 🍎，不会因为忘存、闪退导致辛苦搭的蓝图丢了。

---

## 打开蓝图

3 种方式任选：
1. 顶栏「工程 ▾ → 加载蓝图…」→ 从当前工程内选择蓝图
2. 顶栏「工程 ▾ → 最近工程」→ 点列表里的工程
3. **启动软件会弹出工程选择对话框**（新建工程 / 打开工程 / 最近工程列表）

> 每张蓝图文件是一个 **.bp.json 文件**，可以用文本编辑器打开看结构，也方便备份和版本管理。

---

## 保存蓝图

- **Ctrl+S** 或工具栏 💾 按钮：保存当前修改
- **文件 → 另存为**：另存一份新文件，原文件不变

建议：
- 做了重要改动后顺手 Ctrl+S（赫萝也会提醒你的~）
- 备份蓝图：直接复制 JSON 文件即可
- 模板图片自动存放在工程的 templates/ 子目录，换电脑只需复制整个工程目录即可

---

## 蓝图文件结构（给好奇宝宝）

实际格式（BlueprintGraph.to_dict）：

```json
{
  "nodes": {
    "node_id": {
      "id": "node_id",
      "type": "FindImage",
      "x": 100, "y": 200,
      "data": { "temp_target_title": "...", "threshold": 0.85 }
    }
  },
  "links": [
    { "from_node": "id1", "from_port": "out_exec",
      "to_node": "id2", "to_port": "in_exec", "type": "exec" }
  ],
  "breakpoints": [],
  "metadata": { "name": "我的蓝图", "version": "2.0",
                "dev_scale_x": 1.0, "dev_scale_y": 1.0 }
}
```
注意：① nodes 是字典（非数组）；② 节点参数字段叫 `data`（非 `params`）；③ 连线数组叫 `links`（非 `connections`），字段是 `from_node`/`from_port`/`to_node`/`to_port`；④ 无顶层 `variables` 字段（变量存在 SetVariable 节点的 data 里）。
