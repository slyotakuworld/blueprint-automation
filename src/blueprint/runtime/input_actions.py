import ctypes
import random
import time

SendInput = ctypes.windll.user32.SendInput
PUL = ctypes.POINTER(ctypes.c_ulong)

# 鼠标按键名称映射
MOUSE_BUTTON_MAP = {
    "left": "left",
    "right": "right",
}

# 鼠标事件标志
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120  # 1 格滚轮 = 120 单位（Windows 标准）
# 对外用像素语义，内部按经验换算成滚轮格数（常见浏览器 3 行 × 14px 字高 ≈ 40px/格）
SCROLL_PX_PER_DETENT = 40


class KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class Input_I(ctypes.Union):
    _fields_ = [
        ("ki", KeyBdInput),
        ("mi", MouseInput),
        ("hi", HardwareInput),
    ]


class Input(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("ii", Input_I),
    ]


DIK_CODES = {
    "esc": (0x01, False),
    "enter": (0x1C, False),
    "space": (0x39, False),
    "backspace": (0x0E, False),
    "tab": (0x0F, False),
    "lshift": (0x2A, False),
    "rshift": (0x36, False),
    "lctrl": (0x1D, False),
    "rctrl": (0x1D, True),
    "lalt": (0x38, False),
    "ralt": (0x38, True),
    "capslock": (0x3A, False),
    "a": (0x1E, False),
    "b": (0x30, False),
    "c": (0x2E, False),
    "d": (0x20, False),
    "e": (0x12, False),
    "f": (0x21, False),
    "g": (0x22, False),
    "h": (0x23, False),
    "i": (0x17, False),
    "j": (0x24, False),
    "k": (0x25, False),
    "l": (0x26, False),
    "m": (0x32, False),
    "n": (0x31, False),
    "o": (0x18, False),
    "p": (0x19, False),
    "q": (0x10, False),
    "r": (0x13, False),
    "s": (0x1F, False),
    "t": (0x14, False),
    "u": (0x16, False),
    "v": (0x2F, False),
    "w": (0x11, False),
    "x": (0x2D, False),
    "y": (0x15, False),
    "z": (0x2C, False),
    "1": (0x02, False),
    "2": (0x03, False),
    "3": (0x04, False),
    "4": (0x05, False),
    "5": (0x06, False),
    "6": (0x07, False),
    "7": (0x08, False),
    "8": (0x09, False),
    "9": (0x0A, False),
    "0": (0x0B, False),
    "up": (0xC8, True),
    "down": (0xD0, True),
    "left": (0xCB, True),
    "right": (0xCD, True),
    "pageup": (0xC9, True),
    "pagedown": (0xD1, True),
    "home": (0xC7, True),
    "end": (0xCF, True),
    "insert": (0xD2, True),
    "delete": (0xD3, True),
    "f1": (0x3B, False),
    "f2": (0x3C, False),
    "f3": (0x3D, False),
    "f4": (0x3E, False),
    "f5": (0x3F, False),
    "f6": (0x40, False),
    "f7": (0x41, False),
    "f8": (0x42, False),
    "f9": (0x43, False),
    "f10": (0x44, False),
    "f11": (0x57, False),
    "f12": (0x58, False),
    # Win 键
    "lwin": (0x5B, True),
    "rwin": (0x5C, True),
    # 其他常用键
    "printscreen": (0xE037, True),
    "scrolllock": (0x46, False),
    "numlock": (0x45, False),
    "pause": (0xE046, True),
    # 符号键
    "minus": (0x0C, False),       # - _
    "equals": (0x0D, False),      # = +
    "lbracket": (0x1A, False),    # [ {
    "rbracket": (0x1B, False),    # ] }
    "semicolon": (0x27, False),   # ; :
    "quote": (0x28, False),       # ' "
    "backslash": (0x2B, False),   # \ |
    "comma": (0x33, False),       # , <
    "period": (0x34, False),      # . >
    "slash": (0x35, False),       # / ?
    "backquote": (0x29, False),   # ` ~
}

# 按键别名映射（用户友好名 → DIK_CODES 键名）
KEY_ALIASES = {
    "ctrl": "lctrl",
    "alt": "lalt",
    "shift": "lshift",
    "win": "lwin",
    "super": "lwin",
    "meta": "lwin",
    "return": "enter",
    "esc": "esc",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "bs": "backspace",
    "bksp": "backspace",
    "prtsc": "printscreen",
    "prtscr": "printscreen",
}

KEYDOWN_FLAG = 0x0008
KEYUP_FLAG = 0x000A
EXTENDED_KEY_FLAG = 0x0001


class InputActions:
    """独立的硬件级输入服务。蓝图 Runtime 只需实例化即可使用。"""

    def __init__(self, log_fn=None, is_running_fn=None):
        self._log = log_fn or (lambda *a, **k: None)
        self._is_running = is_running_fn or (lambda: True)
        self.safe_region = None

    def _running(self):
        return bool(self._is_running())

    def set_safe_region(self, region_xywh):
        self.safe_region = region_xywh

    def _send_key_event(self, key, is_down):
        # 别名转换
        key = KEY_ALIASES.get(key, key)
        if key not in DIK_CODES:
            self._log(f"[Input] 未知按键: {key}", "WARN")
            return False
        scan_code, extended = DIK_CODES[key]
        flags = (KEYDOWN_FLAG if is_down else KEYUP_FLAG) | (EXTENDED_KEY_FLAG if extended else 0)
        extra = ctypes.c_ulong(0)
        ii_ = Input_I()
        ii_.ki = KeyBdInput(0, scan_code, flags, 0, ctypes.pointer(extra))
        x = Input(ctypes.c_ulong(1), ii_)
        SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))
        return True

    def key_down(self, key):
        self._send_key_event(key, True)

    def key_up(self, key):
        self._send_key_event(key, False)

    def hw_press(self, key, hold_ms=None, hold_ms_range=(30, 80)):
        if not self._running():
            return
        if hold_ms is None:
            hold_ms = random.randint(int(hold_ms_range[0]), int(hold_ms_range[1]))
        self._send_key_event(key, True)
        time.sleep(hold_ms / 1000.0)
        self._send_key_event(key, False)
        self._log(f"[Input] 按键 {key} 保持 {hold_ms}ms")

    def press_with_pause(self, key, count=1, hold_ms=50, interval_ms=100):
        for i in range(count):
            if not self._running():
                return False
            self.hw_press(key, hold_ms=hold_ms)
            if i < count - 1:
                time.sleep(interval_ms / 1000.0)
        return True

    def _hw_mouse_move(self, x, y):
        """硬件级鼠标移动到绝对屏幕坐标"""
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        left = ctypes.windll.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = ctypes.windll.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = ctypes.windll.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = ctypes.windll.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if width == 0 or height == 0:
            return
        calc_x = int((x - left) * 65535 / width)
        calc_y = int((y - top) * 65535 / height)
        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_ABSOLUTE = 0x8000
        MOUSEEVENTF_VIRTUALDESK = 0x4000
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        extra = ctypes.c_ulong(0)
        ii_ = Input_I()
        ii_.mi = MouseInput(calc_x, calc_y, 0, flags, 0, ctypes.pointer(extra))
        cmd = Input(ctypes.c_ulong(0), ii_)
        SendInput(1, ctypes.pointer(cmd), ctypes.sizeof(cmd))

    def _hw_mouse_down(self, button="left"):
        """硬件级鼠标按下"""
        if button == "right":
            flags = MOUSEEVENTF_RIGHTDOWN
        else:
            flags = MOUSEEVENTF_LEFTDOWN
        extra = ctypes.c_ulong(0)
        ii_ = Input_I()
        ii_.mi = MouseInput(0, 0, 0, flags, 0, ctypes.pointer(extra))
        cmd = Input(ctypes.c_ulong(0), ii_)
        SendInput(1, ctypes.pointer(cmd), ctypes.sizeof(cmd))

    def _hw_mouse_up(self, button="left"):
        """硬件级鼠标释放"""
        if button == "right":
            flags = MOUSEEVENTF_RIGHTUP
        else:
            flags = MOUSEEVENTF_LEFTUP
        extra = ctypes.c_ulong(0)
        ii_ = Input_I()
        ii_.mi = MouseInput(0, 0, 0, flags, 0, ctypes.pointer(extra))
        cmd = Input(ctypes.c_ulong(0), ii_)
        SendInput(1, ctypes.pointer(cmd), ctypes.sizeof(cmd))

    def _hw_mouse_wheel(self, detents):
        """硬件级鼠标滚轮滚动。
        detents 正=向前(上) 负=向后(下) 1=滚轮咔哒1格
        为了体感渐进（不瞬移），拆成 1 格/次 SendInput，每格间隔 ~25ms 模拟人类连续滚动。
        """
        if detents == 0:
            return
        direction = 1 if detents > 0 else -1  # 单格方向 +1/-1
        total_steps = abs(int(detents))
        one_step = WHEEL_DELTA * direction
        for i in range(total_steps):
            if not self._running():
                return  # 蓝图停止/暂停时立即中止后续滚动
            extra = ctypes.c_ulong(0)
            ii_ = Input_I()
            ii_.mi = MouseInput(0, 0, one_step, MOUSEEVENTF_WHEEL, 0, ctypes.pointer(extra))
            cmd = Input(ctypes.c_ulong(0), ii_)
            SendInput(1, ctypes.pointer(cmd), ctypes.sizeof(cmd))
            # 间隔 20~30ms 让系统逐帧渲染，呈现渐进翻页体感
            time.sleep(0.02 + 0.005 * random.random())

    def _mouse_move(self, x, y):
        """公开的鼠标移动方法（供 drag 等使用）"""
        self._hw_mouse_move(int(x), int(y))

    def scroll(self, pos, dy_px=0):
        """在指定位置滚动鼠标滚轮。dy_px 对外是像素估算值，内部会换算成滚轮格数再发事件。"""
        if not self._running():
            return
        if pos:
            if len(pos) == 4:
                x, y, w, h = pos
                cx = int(x) + int(w) // 2
                cy = int(y) + int(h) // 2
            else:
                cx, cy = int(pos[0]), int(pos[1])
            self._hw_mouse_move(cx, cy)
            time.sleep(0.03)
        if dy_px != 0:
            # 像素 → 滚轮格数（每格经验值 40px）；极小值保证至少 ±1 格，让小距离也有体感
            import math
            detents_raw = dy_px / SCROLL_PX_PER_DETENT
            detents = int(round(detents_raw))
            if detents == 0 and dy_px != 0:
                detents = 1 if dy_px > 0 else -1
            self._hw_mouse_wheel(detents)

    @staticmethod
    def _apply_random_offset(pos, radius):
        if not pos or radius <= 0:
            return pos
        ox = random.randint(-radius, radius)
        oy = random.randint(-radius, radius)
        return (int(pos[0]) + ox, int(pos[1]) + oy)

    def game_click(
        self,
        pos,
        double=False,
        random_offset_radius=8,
        hold_ms_range=(30, 80),
        post_delay_range=(80, 200),
        repeat=1,
        button="left",
    ):
        if not self._running() or not pos:
            return None
        if len(pos) == 4:
            x, y, w, h = pos
            cx = int(x) + int(w) // 2
            cy = int(y) + int(h) // 2
            base_pos = (cx, cy)
        else:
            base_pos = (int(pos[0]), int(pos[1]))

        repeat_n = max(1, int(repeat or 1))
        last_actual = None
        last_hold = 0
        last_post = 0
        for r in range(repeat_n):
            if not self._running():
                return None
            actual_pos = self._apply_random_offset(base_pos, random_offset_radius)
            x, y = actual_pos
            last_actual = actual_pos
            self._hw_mouse_move(x, y)
            time.sleep(0.05)
            click_count = 2 if double else 1
            for i in range(click_count):
                if not self._running():
                    return None
                hold_ms = random.randint(int(hold_ms_range[0]), int(hold_ms_range[1]))
                last_hold = hold_ms
                self._hw_mouse_down(button)
                time.sleep(hold_ms / 1000.0)
                self._hw_mouse_up(button)
                if i < click_count - 1:
                    time.sleep(0.08)
            post_delay_ms = random.randint(int(post_delay_range[0]), int(post_delay_range[1]))
            last_post = post_delay_ms
            if r < repeat_n - 1:
                time.sleep(post_delay_ms / 1000.0)

        repeat_txt = f" x{repeat_n}" if repeat_n > 1 else ""
        self._log(
            f"[Input] 点击 ({base_pos[0]},{base_pos[1]}) double={double}{repeat_txt} "
            f"hold~{last_hold}ms post~{last_post}ms"
        )
        return last_actual
