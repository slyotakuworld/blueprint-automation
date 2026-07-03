"""
全局热键管理（封装 pynput.keyboard.GlobalHotKeys）：
  * 规范化（交给 user_settings.normalize_pynput_hotkey）
  * register / unregister / replace 一键切换
  * 线程安全：pynput回调是后台线程，所以通过 QObject Signal 方式跨线程切回主线程回调业务。

典型用法：
  mgr = GlobalHotkeyManager(parent_qobj_or_None)
  mgr.set_callback("CAPTURE_TARGET", lambda: print("fired"))   # 业务回调（将在主线程执行）
  ok = mgr.register("<ctrl>+<f8>")
  ...
  mgr.replace_hotkey("<ctrl>+<f9>")   # 先stop旧，再注册新
  mgr.unregister()   # 关闭时释放
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

try:
    from PySide6.QtCore import QObject, QThread, Signal, QTimer
    _HAS_QT = True
except Exception:  # pragma: no cover
    _HAS_QT = False


from ..config.user_settings import normalize_pynput_hotkey


class HotkeyBridge:
    """无Qt场景下的兜底：不切线程，直接在回调线程调用（仅用于非GUI环境测试）。"""

    def __init__(self):
        self._cbs: Dict[str, Callable[[], None]] = {}

    def set_callback(self, key: str, cb: Callable[[], None]):
        self._cbs[str(key)] = cb

    def fire(self, key: str):
        cb = self._cbs.get(str(key))
        if callable(cb):
            try:
                cb()
            except Exception as e:
                print(f"[HotkeyBridge] fire key={key!r} callback error: {e}")


if _HAS_QT:
    class _QtHotkeyBridge(QObject):
        """通过 Signal 自动切到 Qt 主线程执行业务回调。"""

        fire = Signal(str)  # 热键ID，比如 "CAPTURE_TARGET"

        def __init__(self, parent=None):
            super().__init__(parent)
            self._cbs: Dict[str, Callable[[], None]] = {}
            self.fire.connect(self._on_fired, Qt.QueuedConnection if hasattr(Qt, "QueuedConnection") else Qt.AutoConnection)

        def set_callback(self, key: str, cb: Callable[[], None]):
            self._cbs[str(key)] = cb

        def _on_fired(self, key: str):
            cb = self._cbs.get(str(key))
            if callable(cb):
                try:
                    cb()
                except Exception as e:
                    print(f"[HotkeyBridge(Qt)] fire key={key!r} callback error: {e}")

    # import Qt 后用了 QueuedConnection 常量
    from PySide6.QtCore import Qt  # noqa: F811


class GlobalHotkeyManager:
    def __init__(self, qt_parent: Optional[object] = None):
        self._hotkeys: Dict[str, str] = {}  # id -> normalized pynput hotkey str
        self._listener = None
        self._running = False

        if _HAS_QT and qt_parent is not None:
            try:
                self._bridge: object = _QtHotkeyBridge(parent=qt_parent)
            except Exception:
                self._bridge = HotkeyBridge()
        else:
            self._bridge = HotkeyBridge()

    # ============ 回调 ============
    def set_callback(self, hotkey_id: str, cb: Callable[[], None]):
        """设置某个热键ID对应的业务回调（回调将在Qt主线程或当前线程执行）。"""
        self._bridge.set_callback(str(hotkey_id), cb)

    # ============ 注册/注销 ============
    def register(self, hotkey_id: str, hotkey_str: str) -> bool:
        """
        注册一个热键。若之前已注册过同一个hotkey_id，将先注销旧的再注册新的。
        注册完成后如果listener没启动会自动start。
        返回注册成功与否（失败不会影响其他已注册热键）。
        """
        hk_norm = normalize_pynput_hotkey(hotkey_str)
        # 如果 listener 已经在跑，必须先 stop 再重注册（pynput GlobalHotKeys 不支持运行时修改 hotkey map）
        need_restart = self._running
        if need_restart:
            self._stop_only()
        self._hotkeys[str(hotkey_id)] = hk_norm
        return self._start_only()

    def replace_hotkey(self, hotkey_id: str, new_hotkey_str: str) -> bool:
        """替换某个热键（先停listener -> 改map -> 再重启）。"""
        return self.register(hotkey_id, new_hotkey_str)

    def unregister(self):
        """彻底注销全部热键 + stop listener（程序退出时调用）。"""
        self._hotkeys = {}
        self._stop_only()

    def pause_all(self):
        """【临时屏蔽】停止所有全局热键的 pynput listener，但保留 _hotkeys 映射。
        用于：设置界面录制快捷键时，防止录制的按键刚好触发已注册的全局热键。
        与 resume_all() 配对使用。"""
        self._stop_only()

    def resume_all(self) -> bool:
        """【恢复】pause_all 之后重新启动 listener（保留的 _hotkeys 完整不变）。"""
        return self._start_only()

    def current_hotkey(self, hotkey_id: str) -> Optional[str]:
        return self._hotkeys.get(str(hotkey_id))

    # ============ 内部 ============
    def _build_hotkey_map(self) -> Dict[str, Callable[[], None]]:
        result: Dict[str, Callable[[], None]] = {}
        for hid, hk in self._hotkeys.items():
            # 捕获 hid (闭包坑：不能直接在 lambda 里引用循环变量 hid)
            def _mk_cb(hotkey_id_=hid):
                def cb():
                    try:
                        if hasattr(self._bridge, "fire") and isinstance(self._bridge, _QtHotkeyBridge if _HAS_QT else type(None)):  # type: ignore[attr-defined]
                            self._bridge.fire.emit(hotkey_id_)  # type: ignore[attr-defined]
                        else:
                            self._bridge.fire(hotkey_id_)
                    except Exception as e:
                        print(f"[GlobalHotkeyManager] _mk_cb error for {hotkey_id_}: {e}")
                return cb
            result[hk] = _mk_cb()
        return result

    def _start_only(self) -> bool:
        if self._running:
            return True
        if not self._hotkeys:
            return True  # 没热键，不启动
        try:
            from pynput import keyboard
            hk_map = self._build_hotkey_map()
            self._listener = keyboard.GlobalHotKeys(hk_map)
            self._listener.daemon = True
            self._listener.start()
            self._running = True
            return True
        except Exception as e:
            print(f"[GlobalHotkeyManager] start failed: {e}")
            self._listener = None
            self._running = False
            return False

    def _stop_only(self):
        if not self._running:
            return
        try:
            if self._listener is not None:
                try:
                    self._listener.stop()
                except Exception:
                    pass
                try:
                    # pynput keyboard listener 可能需要join一下
                    self._listener.join(timeout=1.0)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._listener = None
            self._running = False
