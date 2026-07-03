"""
用户持久化配置：全局热键/未来其他设置。
负责读写 + 热键格式规范化 + 两种格式互转：
  - pynput 格式:  "<ctrl>+<shift>+<f8>"   （注册热键用）
  - Qt 格式:      "Ctrl+Shift+F8"          （ShortcutRecorder 显示/QKeySequence 用）
"""
import json
import os
import re
from typing import Any, Dict, Optional

from .app_paths import USER_CONFIG_FILE, get_app_dir

# ============================================================
# 快捷键元信息 + 默认值：
# ⚠️ 不要让「全局热键」之间冲突；也尽量避开常见游戏/IDE组合：
#   - 避免 Ctrl+Shift+A(微信/QQ截图)、Ctrl+C/V/Z/X(编辑)、F11(全屏)、F5(Firefox刷新/调试继续)等
#   - 「全局热键」用 GlobalHotkeyManager 注册 → 游戏前台也能按到（跨窗口生效）
#   - 「局部快捷键」用 QAction.setShortcut → 只有主窗口激活时生效（不抢其它 app）
# ============================================================

# ---------- 默认热键（pynput 格式）----------
# 兼容别名：CAPTURE 是之前已有的全局截图热键
DEFAULT_HOTKEY_CAPTURE_PYNPUT: str = "<f8>"              # 全局：截目标窗口（F8=截图 是用户一直以来的肌肉记忆）
DEFAULT_GLOBAL_CAPTURE_HOTKEY: str = DEFAULT_HOTKEY_CAPTURE_PYNPUT  # 向后兼容变量名

# 全部快捷键：有序注册 → 用于设置页面逐行显示 / 主窗口统一绑定
# 结构: ( key_id, 中文说明, 默认pynput值, scope: "global"|"local" )
# 【2026-07-03 按用户最新要求：
#   1) 标准快捷键只保留 4 个硬编码：Ctrl+S 保存 / Ctrl+Z 撤销 / Ctrl+C 复制 / Ctrl+V 粘贴
#      （其它 Ctrl+N / O / Shift+S / 0 / 1 等仍然硬编码可用，但不在设置界面展示了）
#   2) 全局热键只保留 4 项：F8截图 / F5运行 / F6暂停 / F7停止
#      → 删除 2 项：✂ 切分UI元素独立快捷键(Ctrl+Shift+X) / ⚠ 紧急杀进程(F6原绑定)
#      → 新增 1 项：⏸ 暂停/继续全局(F6)，复用顶栏按钮的切换逻辑
#   3) 设置界面「🎛 通用」改为开发者介绍文案。】
SHORTCUT_REGISTRY: tuple = (
    # ---- 全局热键（跨窗口，游戏前台也能按到）—— 顺序=优先级（冲突修复时越靠前越优先保留，现在4项没冲突） ----
    ("capture_target",      "🎯 截屏 + 打开切分（全局）",              "<f8>",                 "global"),
    ("run_blueprint",       "▶ 运行蓝图（全局，游戏前台也能按）",        "<f5>",                 "global"),
    ("pause_blueprint",     "⏸ 暂停 / 继续蓝图（全局，游戏前台也能按）", "<f6>",                 "global"),
    ("stop_blueprint",      "■ 优雅停止当前蓝图（全局）",              "<f7>",                 "global"),
)
# key -> 默认值 快速查表
_DEFAULT_BY_KEY: Dict[str, str] = {row[0]: row[2] for row in SHORTCUT_REGISTRY}
# key -> scope 快速查表
_SCOPE_BY_KEY: Dict[str, str] = {row[0]: row[3] for row in SHORTCUT_REGISTRY}
# key -> label 快速查表
_LABEL_BY_KEY: Dict[str, str] = {row[0]: row[1] for row in SHORTCUT_REGISTRY}


# 方便外部判断"这个快捷键要不要注册到GlobalHotkeyManager"
def is_global_shortcut(key_id: str) -> bool:
    return _SCOPE_BY_KEY.get(key_id) == "global"


def shortcut_label(key_id: str) -> str:
    return _LABEL_BY_KEY.get(key_id, key_id)


def all_shortcut_keys() -> list:
    return [row[0] for row in SHORTCUT_REGISTRY]


_DEFAULT_SETTINGS: Dict[str, Any] = {
    "hotkeys": {
        k: v for k, v in _DEFAULT_BY_KEY.items()
    },
    "ui": {
        "theme": "dark",
    },
}


# ============================================================
# 读写
# ============================================================
def read_user_settings() -> Dict[str, Any]:
    """读取用户 settings，缺失字段自动补默认值（不破坏用户文件里已有的其他键）。
    兼容历史遗留格式：文件里只有一个字符串 "<f8>"（早期版本整文件存 capture_target 热键）。
    """
    data: Dict[str, Any] = {}
    path = USER_CONFIG_FILE
    legacy_capture: Optional[str] = None
    try:
        if path and os.path.isfile(path):
            loaded_raw: Any = None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded_raw = json.load(f)
            except (json.JSONDecodeError, OSError):
                try:
                    with open(path, "r", encoding="gbk", errors="ignore") as f:
                        loaded_raw = json.load(f)
                except Exception:
                    loaded_raw = None

            if isinstance(loaded_raw, dict):
                data = loaded_raw
            elif isinstance(loaded_raw, str) and loaded_raw.strip():
                # 兼容 2026-07 之前遗留：整个文件只存 json.dump("<f8>") 这一个 capture_target 热键
                legacy_capture = normalize_pynput_hotkey(loaded_raw.strip())
    except Exception:
        data = {}

    # 回填历史遗留 capture_target
    if legacy_capture:
        data.setdefault("hotkeys", {})
        data["hotkeys"].setdefault("capture_target", legacy_capture)

    # ============================================================
    # 【2026-07-03 历史遗留清理 + 用户自定义热键永久保留策略】
    # ⚠ 这一步必须放在 _merge_defaults 之前执行！
    # 核心原则：
    #   ① 【永远执行】删除 7 个历史遗留非法热键ID（防止用户手动编辑/旧版本写入污染）
    #   ② 【仅首次迁移时执行】强制 4 个全局热键 = 用户最终要求的默认值(F8/F5/F6/F7)
    #      → 触发条件：当前 config 里还留有非法ID  或  缺少 4 个必备ID中的任何一个
    #      → 一旦 config 干净了（4ID都存在且无非法ID），之后用户在 ⚙设置 里自己改的任何值永久保留
    #        不会再被强制覆盖回默认（解决用户最担心的『在设置里改了白改』的问题！）
    # ============================================================
    _INVALID_LEGACY_IDS = {
        "emergency_kill", "split_ui_elements",
        "new_blueprint", "open_blueprint", "save_as_blueprint",
        "zoom_fit", "zoom_reset",
    }
    _CORRECT_4 = dict(_DEFAULT_BY_KEY)   # capture=F8 / run=F5 / pause=F6 / stop=F7（就是 SHORTCUT_REGISTRY 4条）
    changed_for_force_clean = False
    if isinstance(data.get("hotkeys"), dict):
        hk: Dict[str, Any] = data["hotkeys"]
        # ① 先判断当前状态（在删非法ID之前判断，更准确）
        had_invalid = any(b in hk for b in _INVALID_LEGACY_IDS)
        missing_any = not all(k in hk for k in _CORRECT_4.keys())
        # ② 永远清一次非法ID（哪怕用户又手动写回来了，下次启动直接干掉）
        for bad in _INVALID_LEGACY_IDS:
            if bad in hk:
                del hk[bad]
                changed_for_force_clean = True
        # ③ 判断是否需要『强制值 = 默认值』（只要之前有非法ID 或 缺少任何一个必备ID，就说明还没迁移干净）
        need_force_values = (had_invalid or missing_any)
        if need_force_values:
            for k, correct_val in _CORRECT_4.items():
                if hk.get(k) != correct_val:
                    hk[k] = correct_val
                    changed_for_force_clean = True
        data["hotkeys"] = hk
    else:
        # hotkeys 字段根本不存在或类型错误 → 肯定需要强制赋值（由下面的 _merge_defaults 补结构）
        changed_for_force_clean = True

    # 补默认键（不覆盖已有）——必须放在强制清理之后！
    _merge_defaults(data, _DEFAULT_SETTINGS)

    # 如果这次真的做了清理/赋值（不是用户正常改设置的场景）→ 立刻写盘
    if changed_for_force_clean:
        try:
            save_user_settings(data)
        except Exception:
            pass
    # 另外顺带清理一下 recent_projects 里可能存在的 Temp/bp_* 垃圾路径（之前 Smoke 脚本写进去的）
    if isinstance(data.get("recent_projects"), list):
        import tempfile as _tmp
        _tmp_root = _tmp.gettempdir()
        clean_lst = []
        for x in data["recent_projects"]:
            if not isinstance(x, str):
                continue
            try:
                rp = os.path.realpath(os.path.abspath(x))
                tr = os.path.realpath(_tmp_root)
                if os.path.commonpath([rp, tr]) == tr:
                    changed_for_force_clean = True
                    continue  # 临时目录下的假工程，直接扔了
            except Exception:
                pass
            if not os.path.isdir(x):
                changed_for_force_clean = True
                continue  # 目录已经不存在的，也清理掉
            clean_lst.append(x)
        if clean_lst != data["recent_projects"]:
            data["recent_projects"] = clean_lst
            try:
                save_user_settings(data)
            except Exception:
                pass
    return data


def save_user_settings(settings: Dict[str, Any]) -> bool:
    """保存用户 settings 到 USER_CONFIG_FILE。失败返回 False。"""
    try:
        path = USER_CONFIG_FILE
        if path:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, path)
            except OSError:
                # Windows 上 replace 偶尔因句柄/杀毒失败，直接 rename 兜底
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
                os.rename(tmp, path)
        return True
    except Exception as e:
        print(f"[user_settings] save failed: {e}")
        return False


def get_hotkey_capture_target(settings: Optional[Dict[str, Any]] = None) -> str:
    s = settings if settings is not None else read_user_settings()
    return get_shortcut("capture_target", s)


def set_hotkey_capture_target(settings: Dict[str, Any], pynput_hotkey: str) -> str:
    """设置并规范化后返回（不自动save，由调用方决定时机）。"""
    return set_shortcut(settings, "capture_target", pynput_hotkey)


def get_shortcut(key_id: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """通用：统一读快捷键（取不到时用 SHORTCUT_REGISTRY 对应默认值，并过一次 normalize_pynput_hotkey 保证格式一致。"""
    s = settings if settings is not None else read_user_settings()
    try:
        v = s["hotkeys"][key_id]
    except Exception:
        v = None
    default = _DEFAULT_BY_KEY.get(key_id) or DEFAULT_GLOBAL_CAPTURE_HOTKEY
    hk = v if isinstance(v, str) and v.strip() else default
    return normalize_pynput_hotkey(hk)


def set_shortcut(settings: Dict[str, Any], key_id: str, pynput_hotkey: str) -> str:
    """统一设置快捷键并规范化，返回规范化后的 pynput 热键字符串。"""
    default = _DEFAULT_BY_KEY.get(key_id) or DEFAULT_GLOBAL_CAPTURE_HOTKEY
    if not (isinstance(pynput_hotkey, str) and pynput_hotkey.strip()):
        pynput_hotkey = default
    hk = normalize_pynput_hotkey(pynput_hotkey)
    settings.setdefault("hotkeys", {})[key_id] = hk
    return hk


# ============================================================
# 合并默认值（递归，不覆盖已有键）
# ============================================================
def _merge_defaults(data: Dict[str, Any], defaults: Dict[str, Any]):
    for k, v in defaults.items():
        if k not in data:
            data[k] = v if not isinstance(v, dict) else dict(v)
        elif isinstance(v, dict) and isinstance(data[k], dict):
            _merge_defaults(data[k], v)


# ============================================================
# 规范化 + 互转
# ============================================================

# 修饰键别名映射（小写后比较）→ pynput 标准 token
_MOD_MAP: Dict[str, str] = {
    # control 家族
    "ctrl": "<ctrl>", "control": "<ctrl>", "ctl": "<ctrl>", "<control>": "<ctrl>",
    # win 家族
    "win": "<cmd>", "windows": "<cmd>", "super": "<cmd>", "meta": "<cmd>",
    "cmd": "<cmd>", "command": "<cmd>", "<win>": "<cmd>", "<windows>": "<cmd>",
    "<super>": "<cmd>", "<meta>": "<cmd>", "<command>": "<cmd>",
    # alt 家族
    "alt": "<alt>", "option": "<alt>", "<option>": "<alt>",
    # shift 家族
    "shift": "<shift>", "<shft>": "<shift>",
}

# Qt -> 别名 -> token
_QT_MOD_MAP: Dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "meta": "meta", "super": "meta", "win": "meta", "windows": "meta", "cmd": "meta", "command": "meta",
}

_SPLIT_RE = re.compile(r"[+ \u002b]+")  # 只按 + / 空格 拆分（不要含下划线！否则 print_screen 被拆坏）

# 功能键名集合（pynput/Qt 都需要尖括号/大写）
_SPECIAL_KEYS: set = {
    "f1","f2","f3","f4","f5","f6","f7","f8","f9","f10","f11","f12","f13","f14","f15","f16","f17","f18","f19","f20",
    "esc","escape","tab","space","enter","return","backspace","delete","del","insert","home","end",
    "page_up","page_down","up","down","left","right","pause","break","print_screen","printscreen","prtsc","prtscr",
    "caps_lock","num_lock","scroll_lock","scrolllock",
    "media_play_pause","media_next","media_previous","media_volume_up","media_volume_down","media_volume_mute",
}


def normalize_pynput_hotkey(hk: str) -> str:
    """
    规范化 pynput 热键字符串：
      例:  "Ctrl+F8" / "ctrl + shift + A" / "<Ctrl><Shift>F8"
        → "<ctrl>+<f8>" / "<ctrl>+<shift>+a"
    规则：
      - 修饰键（ctrl/shift/alt/cmd）统一加 <> 并按固定顺序（ctrl→shift→alt→cmd）
      - 单字母/数字：不加 <> （pynput要求"<ctrl>+a"而不是"<ctrl>+<a>"）
      - 功能键（F1..F12 / print_screen / space 等）加 <>
    """
    if not isinstance(hk, str) or not hk.strip():
        return DEFAULT_GLOBAL_CAPTURE_HOTKEY
    raw_in = hk.strip()

    # ---- 阶段1：把 "<xxx><yyy>F8" 这种无分隔符形式 先切 成 "<xxx>+<yyy>+F8" ----
    buf: list[str] = []
    i, n = 0, len(raw_in)
    while i < n:
        ch = raw_in[i]
        if ch == "<":
            j = raw_in.find(">", i)
            if j >= 0:
                buf.append(raw_in[i:j + 1])
                i = j + 1
                continue
        # 非 <> 块：读到下一个 < 或结尾
        j = i
        while j < n and raw_in[j] != "<":
            j += 1
        seg = raw_in[i:j]
        for sub in _SPLIT_RE.split(seg):
            if sub:
                buf.append(sub)
        i = j

    # ---- 阶段2：识别 修饰键 vs 最终键 ----
    mods: list[str] = []
    key_tok = ""
    for t in buf:
        # 去掉 <>
        tt = t.replace("<", "").replace(">", "").lower()
        if not tt:
            continue
        # 是否修饰键？
        mapped = _MOD_MAP.get(tt)
        if mapped is None:
            # 试试别名匹配：control→<ctrl> 这种（_MOD_MAP里有 "<control>" -> "<ctrl>"）——但我们replace过了，
            # 再补一次映射（去掉<后比较）
            for k_alias, v_mod in _MOD_MAP.items():
                clean = k_alias.replace("<", "").replace(">", "").lower()
                if clean == tt:
                    mapped = v_mod
                    break
        if mapped is not None:
            if mapped not in mods:
                mods.append(mapped)
        else:
            key_tok = tt

    if not key_tok:
        return DEFAULT_GLOBAL_CAPTURE_HOTKEY

    # ---- 阶段3：最后一个键 是否需要 <> ----
    # 长度1=字母数字 -> 不加
    # 特殊键 -> 加<>
    # 其他（如 print_screen）-> 加<>
    need_bracket = (len(key_tok) > 1) or key_tok in _SPECIAL_KEYS
    if need_bracket:
        key_part = f"<{key_tok}>"
    else:
        key_part = key_tok

    # 修饰键排序 ctrl→shift→alt→cmd
    order = {"<ctrl>": 1, "<shift>": 2, "<alt>": 3, "<cmd>": 4}
    mods_sorted = sorted(mods, key=lambda m: order.get(m, 99))
    parts = mods_sorted + [key_part]
    return "+".join(parts)


def pynput_to_qkeysequence(hk: str) -> str:
    """
    pynput "<ctrl>+<shift>+<f8>" / "<ctrl>+a" → Qt "Ctrl+Shift+F8" / "Ctrl+A"
    """
    norm = normalize_pynput_hotkey(hk)
    # tokens: <ctrl>, <shift>, <f8>  或  <ctrl>, a
    tokens = [t for t in norm.split("+") if t]
    qt_mods: list[str] = []
    qt_key = ""
    for tok in tokens:
        t = tok.replace("<", "").replace(">", "").lower()
        if t in ("ctrl", "control"):
            qt_mods.append("Ctrl")
        elif t in ("shift",):
            qt_mods.append("Shift")
        elif t in ("alt", "option"):
            qt_mods.append("Alt")
        elif t in ("cmd", "win", "windows", "super", "meta", "command"):
            qt_mods.append("Meta")
        else:
            # 字母（长度1）→ 大写；功能键 → 首字母大写其余小写（f1->F1, print_screen->Print_Screen）
            if len(t) == 1:
                qt_key = t.upper()
            else:
                # 按_分段每个单词首字母大写
                pieces = t.split("_")
                qt_key = "_".join(
                    (p[0].upper() + p[1:]) if p else "" for p in pieces
                )
    order = {"Ctrl": 1, "Shift": 2, "Alt": 3, "Meta": 4}
    qt_mods_sorted = sorted(qt_mods, key=lambda m: order.get(m, 99))
    all_parts = qt_mods_sorted + ([qt_key] if qt_key else [])
    return "+".join(all_parts) if all_parts else "(无)"


def qkeysequence_to_pynput(qks_str: str) -> Optional[str]:
    """
    Qt "Ctrl+Shift+F8" / "Alt + A" → pynput "<ctrl>+<shift>+<f8>" / "<alt>+a"
    无法解析时返回 None。
    """
    if not isinstance(qks_str, str) or not qks_str.strip():
        return None
    raw = qks_str.strip()
    if raw in ("(无)", "", "none", "(未设置)"):
        return None

    # token 拆分：考虑"<Ctrl><Shift>A"这种不带+但带<>的场景
    buf: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "<":
            j = raw.find(">", i)
            if j >= 0:
                buf.append(raw[i:j + 1])
                i = j + 1
                continue
        j = i
        while j < n and raw[j] != "<":
            j += 1
        seg = raw[i:j]
        for sub in _SPLIT_RE.split(seg):
            if sub:
                buf.append(sub)
        i = j

    mods: list[str] = []
    key = ""
    for t in buf:
        tt = t.replace("<", "").replace(">", "").lower()
        if not tt:
            continue
        m = _QT_MOD_MAP.get(tt)
        if m == "ctrl":
            if "<ctrl>" not in mods:
                mods.append("<ctrl>")
        elif m == "shift":
            if "<shift>" not in mods:
                mods.append("<shift>")
        elif m == "alt":
            if "<alt>" not in mods:
                mods.append("<alt>")
        elif m == "meta":
            if "<cmd>" not in mods:
                mods.append("<cmd>")
        else:
            key = tt
    if not key:
        return None

    # 最后键是否需要 <>
    need_bracket = (len(key) > 1) or key in _SPECIAL_KEYS
    if need_bracket:
        key_part = f"<{key}>"
    else:
        key_part = key

    order = {"<ctrl>": 1, "<shift>": 2, "<alt>": 3, "<cmd>": 4}
    mods_sorted = sorted(mods, key=lambda m: order.get(m, 99))
    parts = mods_sorted + [key_part]
    return "+".join(parts)
