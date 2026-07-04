import ctypes
import random
import time

import pydirectinput

SendInput = ctypes.windll.user32.SendInput
PUL = ctypes.POINTER(ctypes.c_ulong)


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
        if key not in DIK_CODES:
            self._log(f"[Input] 未知按键: {key}", "WARN")
            return
        scan_code, extended = DIK_CODES[key]
        flags = (KEYDOWN_FLAG if is_down else KEYUP_FLAG) | (EXTENDED_KEY_FLAG if extended else 0)
        extra = ctypes.c_ulong(0)
        ii_ = Input_I()
        ii_.ki = KeyBdInput(0, scan_code, flags, 0, ctypes.pointer(extra))
        x = Input(ctypes.c_ulong(1), ii_)
        SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))

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
                pydirectinput.mouseDown()
                time.sleep(hold_ms / 1000.0)
                pydirectinput.mouseUp()
                if i < click_count - 1:
                    time.sleep(0.08)
            post_delay_ms = random.randint(int(post_delay_range[0]), int(post_delay_range[1]))
            last_post = post_delay_ms
            if r < repeat_n - 1:
                time.sleep(post_delay_ms / 1000.0)

        if self.safe_region and len(self.safe_region) == 4:
            gx, gy, _, _ = self.safe_region
            self._hw_mouse_move(gx + 5, gy + 5)
        else:
            self._hw_mouse_move(5, 5)
        time.sleep(0.05)
        repeat_txt = f" x{repeat_n}" if repeat_n > 1 else ""
        self._log(
            f"[Input] 点击 ({base_pos[0]},{base_pos[1]}) double={double}{repeat_txt} "
            f"hold~{last_hold}ms post~{last_post}ms"
        )
        return last_actual
