# BlueprintAutomation LLM 助手集成开发文档

> 版本：v2.0 ｜ 日期：2026-07-05 ｜ 当前阶段：UI Stub 完成，LLM 接入预留

---

## 1. 概述

### 1.1 当前定位

BlueprintAutomation 内置赫萝风格通用 AI 桌面助理，定位为「游戏 + 蓝图 + 代码 + 图像」全能助手，通过桌面悬浮球触发，提供快速聊天和完整对话两种交互形态。当前阶段 UI 壳子已全部完成，LLM 调用为打桩 Mock，预留接入位。

### 1.2 核心交互形态

| 形态 | 触发方式 | 入口文件 | 用途 |
|------|---------|---------|------|
| 桌面悬浮球 | 程序启动自动显示 | `floating_ball.py` | 系统级置顶入口（Win32 双保险压过游戏全屏） |
| 快速聊天小气泡 | 右键悬浮球 | `quick_chat_bubble.py` | 轻量聊天 + 截图 + 预设问题 |
| 完整对话大窗口 | 左键悬浮球 | `full_chat_dialog.py` | 7 Tab 全功能工作区 |
| LLM 设置面板 | FullChatDialog 的「助手设置」Tab 跳转 | `llm_settings_panel.py` | provider 配置 + 赫萝人设 + 记忆库 |

### 1.3 设计原则

1. **UI 与 LLM 解耦**：当前所有 LLM 回复都是 Mock 打桩，接入真实 LLM 时只换调用层，不动 UI
2. **配置 JSON 化**：人设预设 / UI 默认值 / 蓝图输出参数全部由 `config/llm_defaults.json` 管理（JSON 必须存在且结构完整，否则直接报错）
3. **保险机制**：严禁往 `src/` 目录写任何东西，蓝图 JSON 走 `BlueprintStore.save()` 正常路径
4. **每个 Tab 独立系统提示词**：自由聊天 / 蓝图生成 / 代码助手三个对话型 Tab 各自使用独立的 system prompt，互不干扰
5. **赫萝风格统一**：固定奶茶白日赫萝主题 + 42 张赫萝主题资源（16 张静态头像 / 23 张功能图标 / 3 张 Animated WebP 动画头像）

---

## 2. 整体架构

### 2.1 模块关系图

```
┌───────────────────────────────────────────────────────────────────────┐
│                      BlueprintAutomation 主程序                       │
│                                                                       │
│  ┌──────────────────┐                                                 │
│  │   悬浮球          │ ← 系统级 TOPMOST（Win32 + Qt 双保险）            │
│  │  floating_ball.py│                                                 │
│  └─┬────────────────┘                                                 │
│    │ 左键                  │ 右键                                      │
│    ▼                      ▼                                            │
│  ┌────────────┐    ┌──────────────┐                                  │
│  │ 完整对话    │    │ 快速聊天小气泡 │                                  │
│  │ FullChatDlg│    │ QuickChatBub │                                  │
│  └─────┬──────┘    └──────┬───────┘                                  │
│        │                  │                                            │
│        │  ┌───────────────┘                                          │
│        │  │ 共享同一份聊天记录（QuickChatBubble.sync_from）            │
│        ▼  ▼                                                          │
│  ┌──────────────────────────────────────────┐                        │
│  │  7 Tab 工作区（仅自由聊天/蓝图生成已接消息流） │                        │
│  │  1. 💬 自由聊天   2. 📋 蓝图生成                │                        │
│  │  3. 🖼 图像分析   4. 🎮 游戏辅助                │                        │
│  │  5. 📝 代码助手   6. 📜 对话历史                │                        │
│  │  7. ⚙ 助手设置  → 弹出 LLMSettingsPanel        │                        │
│  └──────────────────────────────────────────┘                        │
│                                                                       │
│  ┌──────────────────────────────────────────┐                        │
│  │  config/llm_defaults.json                │                        │
│  │  ├ ui 域：主题/置顶/stream/SSL           │                        │
│  │  ├ persona 域：3 套赫萝人设预设          │                        │
│  │  └ blueprint 域：校验/草稿/重试次数      │                        │
│  └──────────────────────────────────────────┘                        │
└───────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  （未来接入）
                ┌──────────────────────────────┐
                │     外部 LLM API 服务商      │
                │  OpenAI 兼容 / Ollama 本地    │
                └──────────────────────────────┘
```

### 2.2 文件清单

```
src/blueprint/llm/
├── __init__.py
└── ui/
    ├── __init__.py
    ├── _holo_theme.py          # 赫萝主题色板 + QSS 生成器（固定奶茶白日）
    ├── _icon_loader.py         # 赫萝 Icon 资源加载 + lru_cache
    ├── floating_ball.py        # 桌面悬浮球（系统级置顶 + Win32 双保险）
    ├── full_chat_dialog.py     # 完整对话大窗口（7 Tab + ChatBubble）
    ├── quick_chat_bubble.py    # 快速聊天小气泡
    └── llm_settings_panel.py   # LLM 设置面板（5 Tab）
```

**资源目录**：`images/llm_icons/` + `images/*.webp`（42 张赫萝主题资源：16 张静态头像 / 23 张功能图标 / 3 张 Animated WebP 动画头像）
**配置文件**：`config/llm_defaults.json`（人设预设 + UI 默认值 + 蓝图输出参数）

---

## 3. UI 模块详细设计

### 3.1 悬浮球（`floating_ball.py`）

**职责**：系统级置顶入口，压过游戏全屏窗口显示。

**关键设计**：
- 不设 parent（独立顶层窗口，避免主窗口最小化时连带隐藏）
- 窗口 flags = `Qt.Window | FramelessWindowHint | WindowStaysOnTopHint | Qt.Tool`
- Win32 双保险：`user32.SetWindowPos(..., HWND_TOPMOST, ...)` + QTimer 500ms 重刷
- `WS_EX_TOOLWINDOW + WS_EX_NOACTIVATE`：不占任务栏、不抢前台焦点
- Animated WebP 头像 + 1.0↔1.03 呼吸缩放 + ±1° 摇摆

**交互**：
- 左键单击（无拖动） → 打开 FullChatDialog
- 右键单击 → 在悬浮球旁弹出 QuickChatBubble（自动判断左右侧，不遮挡球，避屏边界）
- 左键拖动 → 移动悬浮球（无吸附功能，停在哪就在哪）

**LLM 设置面板入口**：通过 FullChatDialog 的「助手设置」Tab 跳转

**状态切换 API**（预留）：`set_avatar_state(state)`，state ∈ `idle / thinking / happy / sleep / sad`

### 3.2 完整对话大窗口（`full_chat_dialog.py`）

**职责**：赫萝全能助理主工作区，7 Tab 架构。

**结构**：
```
┌──────────────────────────────────────────────────────────────┐
│ [左 220px 侧栏 holoSideFrame]  │ [右 主区]                      │
│ ┌─看板娘 64x64 avatar_holo ─┐ │ 顶部操作条（新建/历史会话2赫萝Icon）  │
│ │ 赫萝·女仆助理 Lv.99 ♡     │ │ ───────────────────────────── │
│ ├─────────────────────────┤ │ 聊天消息列表（ChatBubble 重写）│
│ │ [icon_chat]    自由聊天  │ │ ───────────────────────────── │
│ │ [icon_blueprint] 蓝图生成│ │ 蓝图输入区（含状态卡片）       │
│ │ [icon_image]    图像分析│ │ ───────────────────────────── │
│ │ [icon_game]    游戏辅助 │ │ 输入卡片：                    │
│ │ [icon_code]    代码助手│ │  [📷][📂][🎤] [输入] [🍎发送圆]│
│ │ [icon_history] 对话历史│ │                                │
│ │ [icon_settings]助手设置│ │                                │
│ ├─────────────────────────┤ │                                │
│ │ [icon_sleep] 休息模式 ⏱│ │                                │
│ └─────────────────────────┘ │                                │
└──────────────────────────────────────────────────────────────┘
```

**7 Tab 说明**：

| # | Tab | 状态 | 描述 |
|---|-----|------|------|
| 1 | 💬 自由聊天 | 已接消息流 | 不限主题闲聊，含推荐话题卡，专属系统提示词 |
| 2 | 📋 蓝图生成 | 已接消息流 | 自然语言 → 蓝图 JSON，顶部「新建会话/历史会话」按钮，专属系统提示词 |
| 3 | 🖼 图像分析 | Stub | 拖拽区 + 截图/上传，未来接 OCR + VLM |
| 4 | 🎮 游戏辅助 | Stub | 4 个卡片：攻略问答 / 问题诊断 / 资讯速递 / 游戏推荐（不涉及蓝图功能） |
| 5 | 📝 代码助手 | Stub | 上代码框 / 下输出 + 顶部「查找 Bug / 解释代码 / 重构风格」3 按钮，专属系统提示词 |
| 6 | 📜 对话历史 | Stub | 会话列表（按时间归档） |
| 7 | ⚙ 助手设置 | 跳转入口 | 跳转到 LLMSettingsDialog |

**ChatBubble 气泡组件**（`full_chat_dialog.py` 内嵌）：
- 用户气泡：右侧，金棕渐变（赫萝发色），文本色 `maid_black`（深棕黑），圆形 👤 头像
- 赫萝气泡：左侧，奶白渐变，圆形赫萝头像，右下角 3 个快捷按钮（重写/导入/复制，使用 QStyle 标准图标 + 文字标签）
- 4 圆角胶囊形（无尖角）
- 文本垂直居中（短文本）/ 顶边对齐（长文本），`Qt.TextWordWrap | Qt.TextWrapAnywhere` 换行
- 超长文本安全网：bh 严格限制在 widget 实际分配高度内
- sizeHint / paintEvent / estimate_height 三条链路共用同一宽度基准（`_hint_width`）

**关键滚动机制**：
- QListWidget 必须 `setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)` + `setUniformItemSizes(False)`，否则超长气泡无法滚到底
- item.sizeHint 必须 = widget.sizeHint（不再绕 estimate_height 间接估算）

### 3.3 快速聊天小气泡（`quick_chat_bubble.py`）

**职责**：悬浮球旁的轻量聊天窗口，共享 FullChatDialog 的自由聊天会话记录。

**结构**：
```
┌─────────────────────────────┐
│ 🐺 [动画头像] 赫萝想跟你说~  │  ← 顶部标题（无关闭按钮，点外部自动关）
├─────────────────────────────┤
│ 聊天消息列表（复用 ChatBubble）│
├─────────────────────────────┤
│ [输入框]                    │
├─────────────────────────────┤
│ [📷 截图][🎤 语音][📋 预设▼] [🍎发送] │
└─────────────────────────────┘
```

**关键设计**：
- `WA_TranslucentBackground=True`（四角无纯色底）
- 顶部动画头像：`images/工作女仆赫萝.webp`（QMovie 驱动，加载失败回退到静态图）
- 预设问题下拉：7 个常见问题（选择后填入输入框，下拉回到占位标题）
- 弹出位置：紧贴悬浮球左/右侧（自动判断），不遮挡球，自动避屏边界
- 共享聊天记录：`sync_from(full_dialog)` 从 FullChatDialog 拉取消息；用户发送时回写到 FullChatDialog
- 滚动模式：`ScrollPerPixel` + `setUniformItemSizes(False)`（与 FullChatDialog 一致）

### 3.4 LLM 设置面板（`llm_settings_panel.py`）

**职责**：LLM provider 配置 + 赫萝人设 + 记忆库 + 使用说明。

**结构**：
```
顶部总开关卡片：
  左：96x96 avatar_holo（圆形大头像）
  中：☑ 启用赫萝智能助理
  右：状态胶囊「🐺 待命 / 🛌 关闭」

Tab 1: 🤖 模型服务商
  · provider 下拉：仅 OpenAI / Ollama（index 1 = Ollama，其他 = OpenAI）
  · API Key 必填（Ollama 除外），无环境变量 fallback
  · 「拉取模型列表」按钮：API Key 为空时弹警告并 abort
  · 对话模型下拉：拉取后选择

Tab 2: ⚙ 通用参数
  · 温度 / 最大 tokens / 超时
  · stream_output / skip_ssl_verify 由 JSON 管理（无 UI checkbox）

Tab 3: 🎭 赫萝人设
  · 3 套预设下拉：女仆赫萝 / 游戏宅赫萝 / 贤狼赫萝
  · 展示卡：性格 / 关键字 / 问候语 / 系统指令
  · 自称「咱」、称呼「汝」由 JSON forced_self / forced_you 强制，不可手动修改

Tab 4: 🧠 记忆库
  · 3 个入口卡片：用户档案 / 全局人设 / 常玩游戏配置
  · 记忆开关 4 项：窗口位置 / 常玩游戏 / 常用快捷键 / 会话上下文
  · 导入 / 导出 JSON

Tab 5: 💡 使用说明
  · 赫萝看板娘插图 + 赫萝口吻说明
```

**关键约束**：
- TabBar 不允许横向滚动，所有 Tab 一屏可见
- 含 QScrollArea，确保窗口高度变小时底部按钮可访问
- 信号 `llm_enabled_changed`：切换启用开关时实时触发悬浮球显示/隐藏

---

## 4. 配置管理

### 4.1 `config/llm_defaults.json`

**职责**：人设预设 + UI 默认值 + 蓝图输出参数（JSON 必须存在且结构完整，缺失直接抛异常）。

**顶层结构**：
```json
{
  "ui": {
    "theme": "day",
    "topmost": true,
    "stream_output": true,
    "skip_ssl_verify": true
  },
  "blueprint": {
    "validate_blueprint": true,
    "autosave_draft": true,
    "max_attempts": 3
  },
  "persona": {
    "forced_self": "咱",
    "forced_you": "汝",
    "presets": [
      {
        "preset_idx": 0,
        "preset_name": "女仆赫萝",
        "preset_label": "女仆赫萝（默认 · 听话又傲娇）",
        "trait_idx": 0,
        "trait_name": "傲娇",
        "traits": "...",
        "greeting": "...",
        "system_prompt": "..."
      }
    ]
  }
}
```

**配置加载策略**：
- JSON 不存在 → 直接抛 `FileNotFoundError`
- JSON 结构不完整（缺 ui / blueprint / persona 任一顶层域）→ 直接抛 `ValueError`
- JSON 改动后需重启应用生效

### 4.2 `config/user_settings.py`

**LLM 相关字段**：
- `enabled: bool`（默认 True，v1 用户 `enabled=False` 自动升级为 True）
- `active_provider: str`
- `providers: Dict[str, LLMProviderEntry]`（仅 OpenAI / Ollama）
- `api_key: str`（不读取环境变量）

### 4.3 关键约束

- API Key 必填，留空无法拉取模型列表和调用
- stream_output / skip_ssl_verify 默认 True，由 JSON 管理（面板上无 checkbox）
- 蓝图输出参数（自动校验/修复、自动保存草稿、单次最大尝试次数 1~10）由 JSON blueprint 域管理（面板上无相关设置）
- LLM 服务商选择用 index 判断（index 1 = Ollama，其他 = OpenAI），不用显示名匹配

---

## 5. 主题与图标系统

### 5.1 固定主题配色（`_holo_theme.py`）

**对外 API**：
- `get_theme(name: str = "day") -> dict[str, str]`：取色板字典（name 为历史兼容参数，实际固定返回 day）
- `build_qss(theme_name: str) -> str`：生成通用 QSS 片段

**主题**：固定奶茶白日：赫萝金棕发色 + 苹果红强调 + 奶茶背景

**关键色板 key**：
| key | 用途 |
|-----|------|
| `bg_root` | 对话框主背景 |
| `bg_card` | 卡片/气泡外框 |
| `primary` | 赫萝发色金棕（主色） |
| `accent` | 赫萝苹果红（强调 / 发送按钮） |
| `maid_black` | 女仆装黑（标题色 / 用户气泡文本色） |
| `bubble_user_0/1` | 用户气泡渐变（发色） |
| `bubble_holo_0/1` | 赫萝气泡渐变（奶白） |
| `text_main` | 正文文字 |
| `text_on_primary` | 主色按钮上的文字 |

### 5.2 Icon 资源（`_icon_loader.py` + `images/llm_icons/` + `images/*.webp`）

**对外 API**：
- `icon_path(filename: str) -> str`：返回绝对路径
- `pixmap(filename: str, size: int = 32) -> QPixmap`：按尺寸缩放
- `icon(filename: str, size: int = 24) -> QIcon`：返回 QIcon
- `circular_pixmap(filename: str, size: int = 32) -> QPixmap`：圆形裁切 + 1px 奶茶描边

**资源清单**（合计 42 张，均为赫萝主题风格 Q 版原画）：

| 分类 | 数量 | 文件名 | 用途 |
|------|------|--------|------|
| 侧栏 Tab | 7 | `icon_chat / blueprint / image / game / code / history / settings.png` | FullChatDialog 侧栏 Tab 图标 |
| 状态·情绪 | 5 | `icon_thinking / happy / sleep / sad / typing.png` | 悬浮球状态 / 打字指示器 |
| 工具按钮 | 11 | `icon_screenshot / upload / send / clear / retry / import / voice / emoji / fullwindow / close / memory.png` | 输入条 / 顶部操作条 / 设置面板按钮 |
| **功能图标小计** | **23** | | |
| 基础头像 | 2 | `avatar_holo.png / avatar_holo_small.png` | 兜底大图 / 小头像缩略 |
| 情绪·全身头像 | 14 | `avatar_holo_apple / bigapple / blush / crying / curtsy / fullbody / holding / joyful / pondering / sleepy / surprised / tearful / tsundere / wave.png` | 设置看板娘 / 悬浮球非 idle 状态 / 气泡头像 |
| **静态头像小计** | **16** | | |
| Animated WebP 动画头像 | 3 | `坐姿女仆赫萝.webp / 沉思女仆赫萝.webp / 工作女仆赫萝.webp` | 侧栏大图看板娘 / 悬浮球 idle·thinking / 快速气泡顶部 |

**关键约束**：
- IconLoader 用 `lru_cache(maxsize=64)` 缓存原始大图，避免重复解码 1.8MB PNG
- Holo Icon 必须复制到 `images/llm_icons/`（静态图）或 `images/`（WebP 动画），不能直接引用原目录

---

## 6. LLM 接入预留（未来 Phase 2）

### 6.1 接入策略

当前所有 LLM 回复都是 `_mock_reply` / `_mock_bp_reply` 打桩，接入时只需：

1. 在 `src/blueprint/llm/` 新增 `llm_client.py`（多模型 API 适配层）
2. 在 `src/blueprint/llm/` 新增 `prompt_builder.py`（System Prompt 组装）
3. 把 `_mock_reply` 替换成 `LLMClient.chat()` 调用
4. 流式输出：`LLMClient.chat(stream_callback=...)` 逐 token 回调，UI 打字机效果

### 6.2 Provider 支持

| provider | base_url | 备注 |
|----------|----------|------|
| OpenAI（含所有兼容实现） | 用户配置 | DeepSeek / 通义 / GPT 等都走这个 |
| Ollama | `http://localhost:11434/v1` | 本地部署，免费，隐私好 |

**实现要点**：
- 全部依赖 `openai>=1.0` SDK 的 `OpenAI(base_url=..., api_key=...)` 构造方式
- provider 选择用 index 判断（index 1 = Ollama，其他 = OpenAI）
- stream_output / skip_ssl_verify 由 `llm_defaults.json` 的 `ui` 域管理

### 6.3 系统提示词隔离

三个对话型 Tab 各自使用独立的系统提示词：

| Tab | 系统提示词来源 | 核心定位 |
|-----|---------------|---------|
| 自由聊天 | `llm_defaults.json` persona 域 | 闲聊，不含蓝图相关提示 |
| 蓝图生成 | `llm_defaults.json` persona 域 + 蓝图节点库 | 自然语言 → 蓝图 JSON |
| 代码助手 | `llm_defaults.json` persona 域 + 代码排查指令 | 排查代码 Bug，不涉及蓝图生成 |

### 6.4 蓝图生成与校验闭环（Phase 2 实现）

```
用户输入自然语言
  ↓
PromptBuilder.build_system_prompt()  ← 读 node_library 动态组装节点说明书
  ↓
LLMClient.chat()  ← 多轮生成+校验
  ↓
三轮校验：
  R1: JSON 语法解析
  R2: 节点类型 + 参数合法性（调 runtime/validator.py）
  R3: 连线合法性（exec→exec / 数据类型匹配 / Start 唯一）
  ↓
全部通过 → 返回合法蓝图
  ↓
用户点「导入」→ BlueprintStore.save() → 画布刷新
```

**校验参数**（由 `llm_defaults.json` blueprint 域管理）：
- `validate_blueprint: true`（自动校验/修复）
- `autosave_draft: true`（自动保存草稿）
- `max_attempts: 3`（单次最大尝试次数，范围 1~10）

---

## 7. 关键约束清单

### 7.1 UI 约束
- LLM 设置面板 TabBar 不允许横向滚动，所有 Tab 一屏可见
- LLM 设置面板必须含 QScrollArea，确保底部按钮可访问
- LLM 功能默认启用（`enabled=True`），v1 用户自动升级
- 切换启用开关时悬浮球实时显示/隐藏
- 悬浮球拖动后无吸附功能，停留在用户松开的位置
- ChatBubble 快捷操作按钮使用 QStyle 标准图标 + 文字标签，不使用赫萝 Icon
- 自由聊天 Tab 不含蓝图相关提示内容
- 聊天气泡文本使用 `Qt.TextWordWrap | Qt.TextWrapAnywhere` 换行
- QListWidget 必须 `ScrollPerPixel` + `setUniformItemSizes(False)`

### 7.2 配置约束
- API Key 必填，不读取环境变量
- stream_output / skip_ssl_verify 由 JSON 管理，无 UI checkbox
- 蓝图输出参数由 JSON blueprint 域管理，面板上无相关设置
- provider 选择用 index 判断，不用显示名匹配
- 赫萝人设自称 / 称呼由 JSON forced_self / forced_you 强制，不可手动修改
- JSON 改动后需重启应用生效

### 7.3 工程约束
- 严禁往 `src/` 目录写任何东西（保险机制）
- 蓝图写入只能走 `BlueprintStore.save()`
- 所有 LLM 逻辑集中在 `src/blueprint/llm/` 目录
- QColor 初始化必须用合法参数组合，不能传线宽值
- PaintEvent 必须用 try/finally 确保 `QPainter.end()` 被调用
