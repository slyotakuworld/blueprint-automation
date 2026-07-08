# 🚀 性能优化与调试技巧

蓝图跑慢了？不知道卡在哪一步了？节点出问题又找不到根因？这一节整理常见的调试手段和性能优化方法。

---

## 一、找图慢？这些地方可以提速

### 1. 搜索范围 search_mode 务必用 relative
- relative 模式：只截目标窗口大小（可能 1280×720 = 92 万像素）
- absolute 全屏模式：截 2560×1440 = 368 万像素（慢 4 倍！）
→ 除非找的是桌面/任务栏图标，否则**优先 BindWindow 一次，后面全部 relative**。

### 2. 模板图别截太大
模板图 200×100 像素和 50×30 像素，对"是这个按钮"的辨识度没差多少，但：
- 匹配时间 ≈ (屏幕像素 / 模板像素) × 算法耗时
→ 把模板图截得紧凑一点（按钮本体就行，别把大片背景包进去）。

### 3. match_strategy 能选简单就别选 feature
```
速度排序（从快到慢）：template > gray > hybrid > feature
```
- 按钮颜色永远是一个颜色不会变 → 直接 template
- 按钮颜色会变但形状不变 → gray
- 不确定 → hybrid（大多数场景够了）
- 只有在画面复杂/变形/光照不稳的时候才单独用 feature

### 4. IfImageDetect 参考图太多？用短路模式
20 张 AND 判断，但其实只要第一张没过立刻否决：
```
eval_mode = short_circuit（短路）
```
→ 不用全跑完 20 张，省时间。

### 5. StillnessDetect ROI 选小一点
- 只框「加载条」那一格比框整个屏幕快 50~100 倍。
- sample_interval 别设太低（1 秒足够）。

---

## 二、不知道卡在哪？一步一步看

### 1. 全局执行日志
运行时每条边（▶ 执行线、○ 数据线）会按顺序记录：
```
[Start] 触发 out_done
  → [OpenApp] in_exec 被触发
  OpenApp: 启动 D:\Games\xxx.exe 成功 PID=12345
  → [BindWindow] in_exec 被触发
  BindWindow: 已绑定窗口 0x123456 赫萝的苹果摊
  ...
```
→ **看最后执行到哪一行**，就知道卡住的节点是谁。

### 2. 用断点或临时加「Notify 节点」
蓝图编辑器支持断点调试（节点右键 → 切换断点），也可以在怀疑卡住的节点**前后**都塞一个 Notify：
```
前一个节点 → Notify("开始找图A") → FindImage(A) → Notify("找图A结束")
```
→ 看到"开始找图A"但没看到"找图A结束" → 100% 是 FindImage 还在等超时。

### 3. 找图节点调试三件套
每张新图第一次使用：
1️⃣ **点「测试匹配」按钮**（90% 的问题在这里就暴露了）
2️⃣ 看结果弹窗里的「级联路径」和「得分」
3️⃣ 决定是调阈值还是换算法还是换模板图

### 4. StillnessDetect / SoundDetect 调试开关
- StillnessDetect：先把 stillness_seconds 调得很小（0.2 秒），看能不能过；把 diff_threshold 从 0.01 改到 0.05 试试
- SoundDetect：**开 debug_visual** ✅，看 ASCII 波形里实际 dB 多大

---

## 三、常见错误码 & 解决方法

### 「坐标错乱：找不到文件」（白色半角引号混用 → 全文档里
如果你在输入表达式或代码中有字符引用错误时，会提示：
```
SyntaxError: EOL while scanning string literal
```
→ 多半是中文引号"" ``「」「这些符号混进表达式里了。统一用英文半角双引号 `"` 或单引号 `'`。

### 「变量不存在 KeyError：xxx」
PressKey / Notify / SetVariable compute 表达式里写了 `{xxx}` 或变量名 xxx，但运行时字典里没有：
- 检查上游 SetVariable 是否真的把它设置了
- 检查拼写（大小写敏感！）

### 「FindImage 找不到明明存在的按钮」
按顺序排查：
1. 点「测试匹配」按钮」
2. 看结果：
   - 有得分但低：调阈值
   - 完全没分：算法不适配 → 换算法或重截图
   - 窗口被挡住/最小化
   - search_mode 错（relative/absolute 绑错窗口了
3. 截图是不是 BindWindow 没绑对窗口

### 「Mouse 点歪了点在错地方」
- 90% 是 search_mode（Mouse / coord_mode 错配：
  ```
  FindImage.relative + Mouse.absolute → 必定点歪！
  ```
→ 保持两个节点模式一致，都是 relative 或都是 absolute。

---

## 四、性能/错误速查表 🍎

| 症状 | 90% 可能原因 | 秒修办法 |
|------|-----------|---------|
| 蓝图一跑就秒停，只跑了 Start 没跑别的 | Start 没接下游 in_exec | 连一条线：Start.out_done → 下一节点.in_exec |
| 跑着跑着不动了 | FindImage 在等超时 / Loop infinite 没 break | 日志看到哪停的 → 调小 timeout / 加 break 分支 |
| 跑很快，但没效果（点了像没点） | Mouse/PressKey 没绑窗口，点到别的地方去了 | 先 BindWindow，后面全 relative |
| Notify 弹了，但没消息 | text 写的 {变量}，上游没设 | 用 SetVariable 先设，或者直接写死文字 |
| HTTP 500/401 | 接口地址或 token 错 | 用 Postman/curl 先确定接口本身是通的 |
