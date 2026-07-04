import math
import os
import uuid
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
    QStyle,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ..core.models import Edge, Node, Port, PortDirection, PortType
from ..core.node_library import NODE_LIBRARY


# ============================================================
# 颜色常量
# ============================================================
COL_BG = QColor("#22272E")
COL_BG_DARK = QColor("#1C2128")
COL_GRID = QColor("#2D333B")
COL_SELECTED = QColor("#58A6FF")
COL_HIGHLIGHT_RUN = QColor("#3FB950")

# ========== 线条高亮颜色方案 ==========
# 临时连线（连线中）：暗淡灰，表示还没连上
COL_TEMP_WIRE = QColor("#6E7681")
# 选中节点时关联线条高亮：亮绿色，醒目识别
COL_NODE_LINK_HL = QColor("#39FF14")
# 选中线条本身高亮：橙色，和搜索高亮一致
COL_EDGE_SELECTED_HL = QColor("#FF9800")

COL_EXEC = QColor("#FFFFFF")
COL_BOOL = QColor("#F778BA")
COL_INT = QColor("#79C0FF")
COL_FLOAT = QColor("#A371F7")
COL_TUPLE2 = QColor("#D29922")
COL_TUPLE4 = QColor("#F0B429")
COL_PATH = QColor("#56D4DD")
COL_STRING = QColor("#54AEFF")
COL_ANY = QColor("#8B949E")


# ============================================================
# 辅助工具函数：b64 -> QPixmap（安全版，失败不抛异常）
# ============================================================
def _b64_to_qpixmap_safe(b64_text: str, size: int = 64) -> Optional[QPixmap]:
    """将base64图片转为QPixmap，限定最大长宽为size；失败返回None。"""
    if not b64_text:
        return None
    try:
        import base64, io
        from PIL import Image as PILImage
        data = base64.b64decode(b64_text)
        pil = PILImage.open(io.BytesIO(data))
        pil.thumbnail((size, size), PILImage.LANCZOS)
        if pil.mode not in ("RGBA", "RGB"):
            pil = pil.convert("RGBA")
        w, h = pil.size
        # 转 QImage -> QPixmap
        from PySide6.QtGui import QImage
        if pil.mode == "RGBA":
            qimg = QImage(pil.tobytes("raw", "RGBA"), w, h, QImage.Format_RGBA8888)
        else:
            qimg = QImage(pil.tobytes("raw", "RGB"), w, h, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg.copy())
        return pix
    except Exception:
        return None


def color_for_data(dt: str) -> QColor:
    return {
        "bool": COL_BOOL,
        "int": COL_INT,
        "float": COL_FLOAT,
        "tuple2": COL_TUPLE2,
        "tuple4": COL_TUPLE4,
        "path": COL_PATH,
        "string": COL_STRING,
        "list_path": COL_PATH,
        "any": COL_ANY,
    }.get(dt, COL_ANY)


# ============================================================
# 尺寸常量
# ============================================================
NODE_WIDTH = 200
NODE_HEADER_H = 30
NODE_PORT_H = 22
NODE_ROUND = 8
PORT_RADIUS = 6
PORT_MARGIN_X = 8


# ============================================================
# PortItem
# ============================================================
class PortItem(QGraphicsObject):
    """节点上的端口小圆圈/三角，被按下时开始画线，在另一端口释放则创建连线。"""

    port_pressed = Signal(object)         # 左键按下端口（scene 据此记录"待连线起点"）
    port_right_clicked = Signal(object)   # 右键点击端口（scene 弹上下文菜单）

    def __init__(self, port: Port, parent: "NodeItem"):
        super().__init__(parent)
        self.port = port
        self._node_item = parent
        self.setAcceptHoverEvents(True)
        self._hover = False
        self.setZValue(2)
        flags = self.flags()
        self.setFlags(flags)

    # ---------------- 属性 ----------------
    def is_exec(self) -> bool:
        return self.port.type == PortType.EXEC

    def is_output(self) -> bool:
        return self.port.direction == PortDirection.OUT

    def data_type(self) -> str:
        return self.port.data_type

    def port_name(self) -> str:
        return self.port.name

    def node_item(self) -> "NodeItem":
        return self._node_item

    def compatible_with(self, other: "PortItem") -> bool:
        if other is self or self._node_item is other._node_item:
            return False
        if self.port.type != other.port.type:
            return False
        # 方向必须相反
        if self.port.direction == other.port.direction:
            return False
        # EXEC 只要是 exec 都能接
        if self.port.type == PortType.EXEC:
            return True
        # DATA 必须 data_type 相同或一方为 any
        a, b = self.port.data_type, other.port.data_type
        return a == b or a == "any" or b == "any"

    def global_center(self) -> QPointF:
        return self.mapToScene(self.boundingRect().center())

    # ---------------- 外观 ----------------
    def boundingRect(self) -> QRectF:
        s = PORT_RADIUS * 2 + 4
        return QRectF(-s / 2, -s / 2, s, s)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.is_exec():
            self._paint_exec(painter)
        else:
            self._paint_data(painter)

    def _base_color(self) -> QColor:
        if self.is_exec():
            return COL_EXEC
        return color_for_data(self.port.data_type)

    def _paint_data(self, painter: QPainter):
        col = self._base_color()
        radius = PORT_RADIUS + (1 if self._hover else 0)
        pen = QPen(col)
        pen.setWidth(2 if self._hover else 1)
        painter.setPen(pen)
        painter.setBrush(QBrush(col if self._hover else QColor("#22272E")))
        painter.drawEllipse(QPointF(0, 0), radius, radius)

    def _paint_exec(self, painter: QPainter):
        col = self._base_color()
        r = PORT_RADIUS + 1
        if self.port.direction == PortDirection.OUT:
            tri = QPolygonF([QPointF(r, 0), QPointF(-r * 0.6, -r), QPointF(-r * 0.6, r)])
        else:
            tri = QPolygonF([QPointF(-r, 0), QPointF(r * 0.6, -r), QPointF(r * 0.6, r)])
        pen = QPen(col)
        pen.setWidth(2 if self._hover else 1)
        painter.setPen(pen)
        painter.setBrush(QBrush(col if self._hover else QColor("#22272E")))
        painter.drawPolygon(tri)

    # ---------------- 事件 ----------------
    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton:
            self.port_pressed.emit(self)
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self.port_right_clicked.emit(self)
            event.accept()
            return
        super().mousePressEvent(event)

    # 注：连线创建的释放判定统一由 NodeScene.mouseReleaseEvent 处理
    # （Qt 鼠标抓取机制下，port_released 信号只会发给按下的端口，无法识别目标端口）
    # 因此这里不再需要 mouseReleaseEvent 发射 port_released 信号。


# ============================================================
# NodeItem
# ============================================================
class NodeItem(QGraphicsObject):
    """节点卡片：标题栏 + 端口列表。"""

    node_position_changed = Signal(str, QPointF)
    node_selected_changed = Signal(str, bool)
    request_delete = Signal(str)

    def __init__(self, node: Node):
        super().__init__()
        self.node = node
        self._port_items: Dict[str, PortItem] = {}
        self._running: bool = False
        self._highlighted: bool = False              # 搜索匹配高亮（橙色）
        self._edge_link_highlighted: bool = False    # 关联边被选中时的联动高亮（蓝色虚线）
        self._flash_until_ms: int = 0
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemSendsGeometryChanges
            | QGraphicsItem.ItemIsFocusable
        )
        self.setZValue(1)
        self._build_ports()
        x, y = int(node.pos[0]), int(node.pos[1])
        self.setPos(x, y)

    # ---------------- 结构 ----------------
    def _meta(self):
        return NODE_LIBRARY.get(self.node.type)

    def _summary_text(self) -> str:
        """返回节点关键参数摘要（显示在画布节点端口区域下方）。
        各节点类型显示不同关键信息，便于用户直观识别节点用途。"""
        n = self.node
        p = n.params or {}
        t = n.type
        if t == "OpenApp":
            path = str(p.get("path", "") or "").strip()
            if not path:
                return ""
            name = os.path.basename(path)
            args = str(p.get("args", "") or "").strip()
            return f"▶ {name} {args}".strip()
        if t == "BindWindow":
            parts = []
            title = str(p.get("title", "") or "").strip()
            exe = str(p.get("exe_name", "") or "").strip()
            cls = str(p.get("class_name", "") or "").strip()
            if title:
                parts.append(f"标题:{title}")
            if exe:
                parts.append(f"程序:{exe}")
            if cls:
                parts.append(f"类:{cls}")
            return " | ".join(parts) if parts else ""
        if t == "WebBrowser":
            url = str(p.get("url", "") or "").strip()
            if not url:
                return ""
            if len(url) > 42:
                url = url[:40] + "…"
            return url
        if t == "Command":
            mode = str(p.get("shell_mode", "") or "").strip()
            cap = p.get("capture_streams", True)
            cap_txt = "捕获输出" if cap else "不捕获"
            mode_map = {"cmd_c": "CMD", "powershell": "PowerShell", "no_shell": "直执行"}
            mode_txt = mode_map.get(mode, mode)
            return f"[{mode_txt}] {cap_txt}"
        if t == "HttpRequest":
            method = str(p.get("method", "") or "").strip()
            url = str(p.get("url", "") or "").strip()
            if not url:
                return f"[{method}]" if method else ""
            if len(url) > 36:
                url = url[:34] + "…"
            return f"[{method}] {url}" if method else url
        if t == "Loop":
            infinite = bool(p.get("infinite", False))
            interval = float(p.get("interval_sec", 1.0) or 1.0)
            if infinite:
                return f"∞ 无限循环  间隔{interval:g}s"
            max_it = int(p.get("max_iterations", 5) or 5)
            return f"循环{max_it}次  间隔{interval:g}s"
        if t == "Sequence":
            mode = str(p.get("mode", "short_circuit") or "short_circuit")
            if mode == "complete":
                mode_txt = "完全求值"
            elif mode == "or_true":
                mode_txt = "一真即真"
            else:
                mode_txt = "短路模式"
            interval = float(p.get("interval_sec", 1.0) or 1.0)
            num = int(p.get("num_paths", 3) or 3)
            return f"[{mode_txt}] {num}路径  间隔{interval:g}s"
        if t == "Notify":
            msg = str(p.get("message", "") or "").strip()
            duration = float(p.get("duration_sec", 3.0) or 3.0)
            sound = bool(p.get("play_sound", False))
            popup = bool(p.get("show_popup", False))
            tags = []
            if popup:
                tags.append("弹窗")
            if sound:
                tags.append("🔊")
            tag_txt = f" {' '.join(tags)}" if tags else ""
            msg_preview = msg.replace("\n", " ")[:30]
            if len(msg) > 30:
                msg_preview += "…"
            return f"{duration:g}s{tag_txt} {msg_preview}"
        if t == "VarSwitch":
            from ..core.node_library import parse_varswitch_conditions
            raw = str(p.get("cases", "") or "")
            # 新格式解析
            conds = parse_varswitch_conditions(raw if isinstance(raw, str) else "")
            valid = [c for c in conds if not c.get("error")]
            if valid:
                labels = [c["label"] for c in valid]
                preview = " ".join(labels[:6])
                if len(labels) > 6:
                    preview += f"…+{len(labels)-6}"
                return f"{len(valid)}条件: {preview}"
            return "（未设置条件）"
        if t == "Clipboard":
            mode = str(p.get("mode", "read") or "read")
            mode_map = {"read": "读取", "write": "写入", "clear": "清空"}
            return f"[{mode_map.get(mode, mode)}]"
        if t == "Mouse":
            coord_mode = str(p.get("coord_mode", "relative") or "relative")
            coord_txt = "相对" if coord_mode == "relative" else "绝对"
            action = str(p.get("action", "click") or "click")
            action_map = {
                "click": "点击", "press": "按住", "release": "释放",
                "move": "移动", "drag": "拖拽", "scroll": "滚动",
            }
            action_txt = action_map.get(action, action)
            # 附加关键信息
            extra = ""
            if action in ("click", "press", "release", "drag"):
                btn = str(p.get("button", "left") or "left")
                btn_map = {"left": "左", "right": "右", "middle": "中", "x1": "侧1", "x2": "侧2"}
                extra = btn_map.get(btn, btn)
                if action == "click":
                    cnt = int(p.get("click_count", 1) or 1)
                    if cnt >= 2:
                        extra += f"x{cnt}"
            elif action == "scroll":
                dy = int(p.get("scroll_dy", 0) or 0)
                extra = f"{'上' if dy > 0 else '下'}{abs(dy)}格"
            # 坐标点
            pos_txt = ""
            if action in ("click", "press", "move", "scroll"):
                pos_val = p.get("pos", (0, 0))
                if isinstance(pos_val, (tuple, list)) and len(pos_val) == 2:
                    pos_txt = f" ({int(pos_val[0])},{int(pos_val[1])})"
            elif action == "drag":
                from_val = p.get("from_pos", (0, 0))
                to_val = p.get("to_pos", (0, 0))
                if isinstance(from_val, (tuple, list)) and isinstance(to_val, (tuple, list)):
                    pos_txt = f" ({int(from_val[0])},{int(from_val[1])})→({int(to_val[0])},{int(to_val[1])})"
            return f"[{coord_txt}][{action_txt}] {extra}{pos_txt}".strip()
        if t == "PressKey":
            kb_mode = str(p.get("kb_mode", "text") or "text")
            if kb_mode == "text":
                text = str(p.get("text", "") or "").strip()
                use_paste = bool(p.get("use_paste", True))
                press_enter = bool(p.get("press_enter", False))
                tags = []
                if use_paste:
                    tags.append("粘贴")
                if press_enter:
                    tags.append("↵")
                tag_txt = f" [{' '.join(tags)}]" if tags else ""
                preview = text.replace("\n", " ")[:24]
                if len(text) > 24:
                    preview += "…"
                return f"[输入文本]{tag_txt} {preview}".strip()
            else:
                key_name = str(p.get("key_name", "") or "").strip()
                count = int(p.get("count", 1) or 1)
                cnt_txt = f" x{count}" if count > 1 else ""
                return f"[按键组合] {key_name}{cnt_txt}"
        if t == "Wait":
            delay_type = str(p.get("delay_type", "fixed") or "fixed")
            if delay_type == "random":
                min_s = float(p.get("min_seconds", 1.0) or 1.0)
                max_s = float(p.get("max_seconds", 3.0) or 3.0)
                return f"[随机] {min_s:g}~{max_s:g}s"
            else:
                sec = float(p.get("seconds", 1.0) or 1.0)
                return f"[固定] {sec:g}s"
        return ""

    def _summary_lines(self) -> List[str]:
        """返回多行摘要（用于 SetVariable / VarSwitch / StillnessDetect / IfImageDetect 等需要多行显示的节点）。
        每行一个字符串，节点高度会根据行数自适应。"""
        n = self.node
        p = n.params or {}
        t = n.type
        if t == "StillnessDetect":
            still_s = float(p.get("stillness_seconds", 5.0) or 5.0)
            tout_s = float(p.get("timeout_seconds", 20.0) or 20.0)
            samp_s = float(p.get("sample_interval_seconds", 1.0) or 1.0)
            lines = [
                f"⏱ 静止持续 {still_s:g}s  |  ⏰ 超时 {tout_s:g}s  |  📷 间隔 {samp_s:g}s",
            ]
            roi_raw = p.get("roi", "[0,0,0,0]")
            try:
                if isinstance(roi_raw, str) and roi_raw:
                    import json as _json
                    roi = [int(v) for v in _json.loads(roi_raw)]
                elif isinstance(roi_raw, (list, tuple)) and len(roi_raw) >= 4:
                    roi = [int(v) for v in list(roi_raw)[:4]]
                else:
                    roi = [0, 0, 0, 0]
            except Exception:
                roi = [0, 0, 0, 0]
            if len(roi) >= 4 and roi[2] > 0 and roi[3] > 0:
                x, y, w, h = roi[0], roi[1], roi[2], roi[3]
                lines.append(f"🎯 ROI: ({x},{y}) {w}×{h}")
            else:
                lines.append("🎯 ROI: （未设置）")
            # 如果有预览图，预留预览占位行（实际图像由paint方法绘制）
            b64 = str(p.get("roi_preview_b64", "") or "")
            if b64:
                lines.append("[__PREVIEW__:stillness]")  # 仅用于高度计算
            return lines
        if t == "FindImage":
            algo = str(p.get("match_strategy", "hybrid") or "hybrid")
            algo_map = {"hybrid": "智能混合", "template": "模板匹配", "feature": "特征匹配"}
            algo_txt = algo_map.get(algo, algo)
            thr = float(p.get("threshold", 0.75) or 0.75)
            tout = float(p.get("timeout_seconds", 10.0) or 10.0)
            auto = bool(p.get("auto_scroll", False))
            # 显示 scroll_dy / scroll_dx 具体数值
            if auto:
                sdy = int(p.get("scroll_dy", -500) or -500)
                sdx = int(p.get("scroll_dx", 0) or 0)
                scroll_parts = []
                if sdy != 0:
                    scroll_parts.append(f"Δy{sdy}")
                if sdx != 0:
                    scroll_parts.append(f"Δx{sdx}")
                if not scroll_parts:
                    scroll_parts.append("0")
                scroll_note = "滚动:ON(" + ",".join(scroll_parts) + ")"
            else:
                scroll_note = "滚动:OFF"
            act = str(p.get("after_action", "none") or "none")
            act_map = {
                "none": "动作:无", "click": "动作:单击", "double_click": "动作:双击",
                "right_click": "动作:右键",
            }
            act_txt = act_map.get(act, act)
            # 展示 offset 数值
            if act != "none":
                try:
                    off = p.get("offset", (0, 0))
                    ox, oy = int(off[0] or 0), int(off[1] or 0)
                except Exception:
                    ox, oy = 0, 0
                if ox != 0 or oy != 0:
                    act_txt += f"(+{ox},+{oy})"
                else:
                    act_txt += "(+0,+0)"
            lines = [
                f"算法:{algo_txt}  相似度≥{thr:.2f}  超时{tout:g}s",
                f"{scroll_note}  {act_txt}",
            ]
            # 如果有模板b64预览，加预览占位
            tb = str(p.get("template_b64", "") or "")
            if tb:
                lines.append("[__PREVIEW__:findimg]")
            return lines
        if t == "IfImageDetect":
            mode = str(p.get("eval_mode", "complete") or "complete")
            if mode == "complete":
                mode_txt = "完全求值"
            elif mode == "or_true":
                mode_txt = "一真即真"
            else:
                mode_txt = "短路模式"
            algo = str(p.get("match_strategy", "hybrid") or "hybrid")
            algo_map = {"hybrid": "混合匹配", "template": "模板匹配", "feature": "特征匹配"}
            algo_txt = algo_map.get(algo, algo)
            thr = float(p.get("threshold", 0.9) or 0.9)
            # 图片数
            img_count = 0
            images_raw = p.get("images_json", "[]")
            try:
                if isinstance(images_raw, str) and images_raw:
                    import json as _json
                    imgs = _json.loads(images_raw)
                elif isinstance(images_raw, list):
                    imgs = images_raw
                else:
                    imgs = []
                img_count = len(imgs) if isinstance(imgs, list) else 0
            except Exception:
                img_count = 0
            lines = [
                f"[{mode_txt}]  算法:{algo_txt}  相似度≥{thr:.2f}",
                f"🖼 参考图: {img_count}张",
            ]
            # 如果有图片，加预览标记行（用于高度计算；实际图像由paint绘制）
            if img_count > 0:
                lines.append("[__PREVIEW__:ifimg]")
            return lines
        if t == "SoundDetect":
            mode = str(p.get("judge_mode", "detect_sound") or "detect_sound")
            mode_cn = "检测声音" if mode in ("detect_sound", "sound") else "检测静音"
            tout = float(p.get("timeout_seconds", 5.0) or 5.0)
            sample_ms = int(p.get("sample_ms", 500) or 500)
            thr = float(p.get("threshold_db", -30.0) or -30.0)
            lines = [
                f"🎧 {mode_cn}  |  ⏰ 超时{tout:g}s",
                f"采样{sample_ms}ms  |  阈值{thr:g}dB",
            ]
            return lines
        if t == "SetVariable":
            batch = str(p.get("batch_text", "") or "").strip()
            if not batch:
                return ["（未设置变量）"]
            lines = []
            for line in batch.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name = line.split("=", 1)[0].strip()
                    val = line.split("=", 1)[1].strip()
                    if len(val) > 20:
                        val = val[:18] + "…"
                    lines.append(f"{name} = {val}")
                else:
                    lines.append(line)
                if len(lines) >= 8:
                    lines.append("…")
                    break
            return lines if lines else ["（未设置变量）"]
        if t == "VarSwitch":
            from ..core.node_library import parse_varswitch_conditions
            raw = str(p.get("cases", "") or "")
            conds = parse_varswitch_conditions(raw if isinstance(raw, str) else "")
            lines: List[str] = []
            err_count = 0
            for c in conds:
                if c.get("error"):
                    err_count += 1
                    continue
                lab = str(c.get("label", ""))
                if len(lab) > 40:
                    lab = lab[:38] + "…"
                lines.append(f"● {lab}")
                if len(lines) >= 6:
                    break
            # 还有没显示的条件，追加计数
            shown_ok = len([c for c in conds if not c.get("error")])
            if shown_ok > len(lines):
                lines.append(f"  …还有 {shown_ok - len(lines)} 条条件")
            if err_count > 0:
                lines.append(f"⚠ {err_count} 处格式错误（点「检查格式」查看）")
            if not lines:
                lines.append("（未设置条件）")
            return lines
        return []

    def _has_summary(self) -> bool:
        """是否支持摘要显示区域（用于固定节点高度，避免有/无内容时高度跳变）。"""
        return self.node.type in (
            "OpenApp", "BindWindow", "WebBrowser", "Command", "HttpRequest",
            "Loop", "Sequence", "Notify", "VarSwitch", "Clipboard", "SetVariable",
            "Mouse", "PressKey", "Wait",
            "StillnessDetect", "IfImageDetect", "FindImage", "SoundDetect",
        )

    def _preview_extra_height(self) -> int:
        """如果节点有预览图（StillnessDetect ROI预览 或 FindImage模板 或 IfImageDetect参考图缩略图），返回额外高度。"""
        t = self.node.type
        p = self.node.params or {}
        if t == "StillnessDetect":
            b64 = str(p.get("roi_preview_b64", "") or "")
            if not b64:
                return 0
            return 92  # 单张预览图高度 + 上下padding
        if t == "FindImage":
            # 仅模板预览（搜索区域功能已移除）
            tb64 = str(p.get("template_b64", "") or "")
            has_tpl = bool(tb64)
            if not has_tpl:
                path = str(p.get("template", "") or "")
                import os as _os
                if isinstance(path, str) and path and _os.path.exists(path):
                    has_tpl = True
            return 92 if has_tpl else 0
        if t == "IfImageDetect":
            raw = p.get("images_json", "[]")
            imgs = []
            try:
                if isinstance(raw, str) and raw:
                    import json as _json
                    imgs = _json.loads(raw)
                elif isinstance(raw, list):
                    imgs = raw
            except Exception:
                imgs = []
            if not isinstance(imgs, list) or len(imgs) == 0:
                return 0
            # 每行最多显示4张，每张约60px高 + padding
            cols = 4
            rows = (len(imgs) + cols - 1) // cols
            return 16 + rows * 64
        return 0

    def node_height(self) -> int:
        n_in = max(1, len(self.node.inputs))
        n_out = max(1, len(self.node.outputs))
        rows = max(n_in, n_out)
        h = NODE_HEADER_H + rows * NODE_PORT_H + 10
        # 支持摘要的节点类型固定预留摘要区域高度，保持高度一致
        if self._has_summary():
            # 多行摘要：根据行数动态调整高度
            multi = self._summary_lines()
            # 过滤预览标记行（[__PREVIEW__:*]），这些行的高度由_preview_extra_height控制
            text_lines = [l for l in multi if not (isinstance(l, str) and l.startswith("[__PREVIEW__:"))]
            if text_lines:
                h += 18 + len(text_lines) * 16
            else:
                h += 28
            # 预览图额外高度
            h += self._preview_extra_height()
        return h

    def _build_ports(self):
        h = self.node_height()
        n_in = len(self.node.inputs)
        n_out = len(self.node.outputs)
        in_y0 = NODE_HEADER_H + 5 + (NODE_PORT_H / 2)
        out_y0 = NODE_HEADER_H + 5 + (NODE_PORT_H / 2)
        for i, p in enumerate(self.node.inputs):
            pi = PortItem(p, self)
            pi.setParentItem(self)
            y = in_y0 + i * NODE_PORT_H
            pi.setPos(PORT_MARGIN_X, y)
            self._port_items[p.name] = pi
        for i, p in enumerate(self.node.outputs):
            pi = PortItem(p, self)
            pi.setParentItem(self)
            y = out_y0 + i * NODE_PORT_H
            pi.setPos(NODE_WIDTH - PORT_MARGIN_X, y)
            self._port_items[p.name] = pi

    def port_item(self, port_name: str) -> Optional[PortItem]:
        return self._port_items.get(port_name)

    def all_port_items(self) -> List[PortItem]:
        return list(self._port_items.values())

    # ---------------- 外观 ----------------
    def boundingRect(self) -> QRectF:
        pad = 4
        return QRectF(
            -pad, -pad,
            NODE_WIDTH + pad * 2,
            self.node_height() + pad * 2,
        )

    def global_center(self) -> QPointF:
        """返回节点在场景坐标下的中心点（用于搜索跳转居中）。"""
        return self.mapToScene(self.boundingRect().center())

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(0, 0, NODE_WIDTH, self.node_height()),
            NODE_ROUND, NODE_ROUND,
        )
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0, 0, NODE_WIDTH, self.node_height())
        meta = self._meta()
        title_color = QColor(meta.color) if meta else QColor("#3A3F4B")
        accent = QColor(meta.accent) if meta else QColor("#58A6FF")
        base_title = meta.title if meta else self.node.type
        extra_name = str(self.node.params.get("custom_name", "") or "").strip()
        if extra_name:
            title = f"{self.node.id} · {base_title} · {extra_name}"
        else:
            title = f"{self.node.id} · {base_title}"

        # 选中描边
        is_sel = bool(option.state & QStyle.State_Selected)
        if is_sel:
            painter.setPen(QPen(COL_SELECTED, 2))
        else:
            painter.setPen(QPen(QColor("#2D333B"), 1))
        painter.setBrush(QBrush(QColor(COL_BG_DARK)))
        painter.drawRoundedRect(rect, NODE_ROUND, NODE_ROUND)

        # 标题栏
        header_rect = QRectF(0, 0, NODE_WIDTH, NODE_HEADER_H)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(title_color))
        path = QPainterPath()
        path.addRoundedRect(header_rect, NODE_ROUND, NODE_ROUND)
        clip = QPainterPath()
        clip.addRect(0, 0, NODE_WIDTH, NODE_HEADER_H)
        painter.drawPath(path & clip)
        # 标题文本
        painter.setPen(QPen(QColor("#FFFFFF")))
        f = QFont()
        f.setBold(True)
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(
            QRectF(10, 0, NODE_WIDTH - 20, NODE_HEADER_H),
            Qt.AlignVCenter | Qt.AlignLeft,
            title,
        )

        # 端口标签
        f2 = QFont()
        f2.setPointSize(8)
        painter.setFont(f2)
        pen = QPen(QColor("#C9D1D9"))
        painter.setPen(pen)

        for i, p in enumerate(self.node.inputs):
            y = NODE_HEADER_H + 5 + NODE_PORT_H / 2 + i * NODE_PORT_H
            label = p.label or p.name
            tx = PORT_MARGIN_X + PORT_RADIUS + 4
            painter.drawText(
                QRectF(tx, y - NODE_PORT_H / 2, NODE_WIDTH / 2, NODE_PORT_H),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )
        for i, p in enumerate(self.node.outputs):
            y = NODE_HEADER_H + 5 + NODE_PORT_H / 2 + i * NODE_PORT_H
            label = p.label or p.name
            tx = NODE_WIDTH / 2
            painter.drawText(
                QRectF(tx, y - NODE_PORT_H / 2,
                       NODE_WIDTH / 2 - PORT_MARGIN_X - PORT_RADIUS - 4, NODE_PORT_H),
                Qt.AlignVCenter | Qt.AlignRight,
                label,
            )

        # 关键参数摘要（端口区域下方，浅色小字）
        summary = self._summary_text()
        multi_lines = self._summary_lines()
        # 过滤预览标记行，这些不是真的文本行
        text_lines = [l for l in multi_lines if not (isinstance(l, str) and l.startswith("[__PREVIEW__:"))]
        if summary or text_lines:
            n_in = max(1, len(self.node.inputs))
            n_out = max(1, len(self.node.outputs))
            rows = max(n_in, n_out)
            sy = NODE_HEADER_H + 5 + rows * NODE_PORT_H + 4
            f3 = QFont()
            f3.setPointSize(8)
            painter.setFont(f3)
            painter.setPen(QPen(QColor("#8B949E")))
            cursor_y = sy
            if text_lines:
                # 多行摘要：逐行绘制
                for i, line in enumerate(text_lines):
                    painter.drawText(
                        QRectF(8, cursor_y, NODE_WIDTH - 16, 16),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        line,
                    )
                    cursor_y += 16
            else:
                painter.drawText(
                    QRectF(8, cursor_y, NODE_WIDTH - 16, 22),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    summary,
                )
                cursor_y += 22
            # 预览图绘制（StillnessDetect / FindImage / IfImageDetect）
            t = self.node.type
            p = self.node.params or {}
            if t == "StillnessDetect":
                b64 = str(p.get("roi_preview_b64", "") or "")
                if b64:
                    cursor_y += 8
                    pix = _b64_to_qpixmap_safe(b64, size=72)
                    if pix is not None:
                        px = (NODE_WIDTH - pix.width()) // 2
                        painter.drawPixmap(px, cursor_y, pix)
            elif t == "FindImage":
                # 模板预览（template_b64 或有 template 路径）
                tb64 = str(p.get("template_b64", "") or "")
                # 没有b64但有路径，直接从路径读（慢一些，但兼容性好
                pil_from_path = None
                import os as _os
                if not tb64:
                    path = str(p.get("template", "") or "")
                    if isinstance(path, str) and path and _os.path.exists(path):
                        try:
                            import base64 as _b64, io as _io
                            from PIL import Image as _PILImg
                            _tmp = _PILImg.open(path)
                            _buf = _io.BytesIO()
                            _tmp.save(_buf, format="PNG")
                            tb64 = _b64.b64encode(_buf.getvalue()).decode("ascii")
                        except Exception:
                            tb64 = ""
                if tb64:
                    cursor_y += 8
                    pix = _b64_to_qpixmap_safe(tb64, size=72)
                    if pix is not None:
                        painter.fillRect(12, cursor_y - 2, NODE_WIDTH - 24, 76, QColor("#0D1117"))
                        px = (NODE_WIDTH - pix.width()) // 2
                        painter.drawPixmap(px, cursor_y, pix)
            elif t == "IfImageDetect":
                raw = p.get("images_json", "[]")
                imgs = []
                try:
                    if isinstance(raw, str) and raw:
                        import json as _json
                        imgs = _json.loads(raw)
                    elif isinstance(raw, list):
                        imgs = raw
                except Exception:
                    imgs = []
                if isinstance(imgs, list) and len(imgs) > 0:
                    cursor_y += 8
                    cols = 4
                    thumb_w = (NODE_WIDTH - 16 - (cols - 1) * 4) // cols
                    thumb_h = 56
                    for idx, img in enumerate(imgs):
                        col = idx % cols
                        row = idx // cols
                        bx = 8 + col * (thumb_w + 4)
                        by = cursor_y + row * (thumb_h + 4)
                        b64_img = ""
                        if isinstance(img, dict):
                            b64_img = str(img.get("b64", "") or "")
                        if b64_img:
                            pix = _b64_to_qpixmap_safe(b64_img, size=min(thumb_w, thumb_h))
                            if pix is not None:
                                px = bx + (thumb_w - pix.width()) // 2
                                py = by + (thumb_h - pix.height()) // 2
                                painter.drawPixmap(px, py, pix)
                            else:
                                painter.fillRect(bx, by, thumb_w, thumb_h, QColor("#2D333B"))
                        else:
                            painter.fillRect(bx, by, thumb_w, thumb_h, QColor("#2D333B"))

        # 运行高亮
        if self._running:
            painter.setBrush(Qt.NoBrush)
            pen = QPen(COL_HIGHLIGHT_RUN, 3)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, NODE_ROUND, NODE_ROUND)

        # 搜索匹配高亮（橙色3px）
        if self._highlighted:
            painter.setBrush(Qt.NoBrush)
            pen = QPen(QColor("#ff9800"), 3)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), NODE_ROUND + 1, NODE_ROUND + 1)

        # 关联边被选中时的联动高亮（蓝色虚线2px）
        # 当用户选中一条连线时，两端节点会显示这个高亮，便于在线多时识别关联节点
        if self._edge_link_highlighted:
            painter.setBrush(Qt.NoBrush)
            pen = QPen(QColor("#58A6FF"), 2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), NODE_ROUND + 3, NODE_ROUND + 3)

        # 闪烁（红色3px，到期自动恢复）
        try:
            import time
            now_ms = int(time.time() * 1000)
            if now_ms < self._flash_until_ms:
                painter.setBrush(Qt.NoBrush)
                # 0.5s方波闪烁
                on = ((now_ms // 250) % 2) == 0
                pen = QPen(QColor("#f44336"), 4 if on else 2)
                painter.setPen(pen)
                painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), NODE_ROUND + 2, NODE_ROUND + 2)
                # 触发下一帧重绘，直到到期
                if now_ms < self._flash_until_ms - 50:
                    try:
                        from PySide6.QtCore import QTimer
                        QTimer.singleShot(80, self.update)
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------------- 搜索匹配/闪烁接口 ----------------
    def set_highlighted(self, on: bool):
        """画布搜索框实时高亮：画一圈橙色3px边"""
        on = bool(on)
        if self._highlighted != on:
            self._highlighted = on
            self.update()

    def set_edge_link_highlighted(self, on: bool):
        """关联边被选中时的联动高亮：画一圈蓝色虚线边（问题3）。"""
        on = bool(on)
        if self._edge_link_highlighted != on:
            self._edge_link_highlighted = on
            self.update()

    def flash(self, duration_ms: int = 2000):
        """红色方框闪烁（跳转后强调效果）"""
        try:
            import time
            self._flash_until_ms = int(time.time() * 1000) + max(100, int(duration_ms))
            self.update()
        except Exception:
            pass

    # ---------------- 事件 ----------------
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            p = self.pos()
            self.node.pos = (int(p.x()), int(p.y()))
            self.node_position_changed.emit(self.node.id, p)
        elif change == QGraphicsItem.ItemSelectedChange:
            self.node_selected_changed.emit(self.node.id, bool(value))
        return super().itemChange(change, value)

    def sync_from_node(self):
        """当 node.inputs / node.outputs 变更后（例如 FindAnyImage 动态分支），重建端口。"""
        for pi in self.all_port_items():
            pi.setParentItem(None)
            scene = pi.scene()
            if scene:
                scene.removeItem(pi)
        self._port_items.clear()
        self._build_ports()
        self.prepareGeometryChange()
        self.update()

    # ---------------- 数据接口 ----------------
    def set_running(self, running: bool):
        self._running = bool(running)
        self.update()


# ============================================================
# EdgeItem（贝塞尔曲线）
# ============================================================
class EdgeItem(QGraphicsPathItem):
    """连接两个端口的贝塞尔连线。"""

    def __init__(self, edge: Optional[Edge] = None, parent=None):
        super().__init__(parent)
        self.edge = edge
        self._from_pos = QPointF()
        self._to_pos = QPointF()
        self._from_port: Optional[PortItem] = None
        self._to_port: Optional[PortItem] = None
        self._temp_mode = edge is None
        # ZValue=-2：低于 PortItem(2)/NodeItem(1)，点击时端口永远优先命中，
        # 避免连线命中区盖住端口导致用户点不到端口
        self.setZValue(-2)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._base_color = COL_ANY
        self._hover = False
        # 关联节点被选中时的联动高亮（问题5）：加粗+加亮
        self._node_link_highlighted = False
        self._update_pen()

    def _update_pen(self):
        """线条颜色优先级（从高到低）：
        1. 选中线条本身 → 橙色高亮 COL_EDGE_SELECTED_HL（加粗）
        2. 关联节点被选中 → 亮绿色高亮 COL_NODE_LINK_HL（加粗）
        3. hover → 基础色 lighter 130%
        4. 普通 → 基础色（临时连线默认暗淡灰，悬停端口时变绿/红反馈）
        """
        if self.isSelected():
            # 选中线条本身：橙色高亮 + 加粗
            pen = QPen(COL_EDGE_SELECTED_HL)
            pen.setWidth(3)
            self.setPen(pen)
            return
        if self._node_link_highlighted:
            # 关联节点被选中：亮绿色高亮 + 加粗
            pen = QPen(COL_NODE_LINK_HL)
            pen.setWidth(3)
            self.setPen(pen)
            return
        # 普通状态：基础色（hover 时加亮加粗）
        # 临时连线默认 _base_color=COL_TEMP_WIRE（暗淡灰），悬停端口时由 mouseMoveEvent 改为绿/红
        color = self._base_color if not self._hover else self._base_color.lighter(130)
        width = 2 if not self._hover else 3
        pen = QPen(color)
        pen.setWidth(width)
        self.setPen(pen)

    def set_node_link_highlighted(self, on: bool):
        """关联节点被选中时的联动高亮（问题5）。"""
        on = bool(on)
        if self._node_link_highlighted != on:
            self._node_link_highlighted = on
            self._update_pen()

    def set_base_color(self, color: QColor):
        self._base_color = color
        self._update_pen()

    def set_from_port(self, p: PortItem):
        self._from_port = p
        if p.port.type == PortType.EXEC:
            self.set_base_color(COL_EXEC)
        else:
            self.set_base_color(color_for_data(p.port.data_type))
        self.update_path_from_ports()

    def set_to_port(self, p: PortItem):
        self._to_port = p
        if p.port.type == PortType.EXEC:
            self.set_base_color(COL_EXEC)
        else:
            self.set_base_color(color_for_data(p.port.data_type))
        self.update_path_from_ports()

    def from_port_item(self) -> Optional[PortItem]:
        return self._from_port

    def to_port_item(self) -> Optional[PortItem]:
        return self._to_port

    def update_path_from_ports(self):
        if self._from_port:
            self._from_pos = self._from_port.global_center()
        if self._to_port:
            self._to_pos = self._to_port.global_center()
        self._rebuild_bezier()

    def set_temp_to(self, pos: QPointF):
        self._to_pos = pos
        self._rebuild_bezier()

    def _rebuild_bezier(self):
        """统一用贝塞尔曲线绘制（EXEC/DATA 都用曲线）。
        曲线控制点在水平方向偏移，避免多条线重叠成一根线（问题4）。
        曲线绘制：从端口水平出发，到目标端口水平进入。"""
        p1 = self._from_pos
        p2 = self._to_pos
        # 控制点偏移量：水平方向，至少 40px 保证曲线有明显弧度
        # 距离越远偏移越大，但不超过 200px（避免曲线过于夸张）
        dx = max(40, min(200, abs(p2.x() - p1.x()) * 0.5))
        path = QPainterPath(p1)
        # 全部用三次贝塞尔曲线（cubicTo），不再区分 EXEC/DATA
        # 两条控制点都在水平方向：从 p1 水平向右/左延伸 dx，从 p2 水平向左/右延伸 dx
        # 这样曲线从端口水平出发，到目标端口水平进入，符合节点连线视觉习惯
        path.cubicTo(
            QPointF(p1.x() + dx, p1.y()),
            QPointF(p2.x() - dx, p2.y()),
            p2,
        )
        self.setPath(path)
        # 触发 boundingRect 重算（shape() 命中区依赖 path）
        self.prepareGeometryChange()

    def shape(self) -> QPainterPath:
        """重写：命中检测区域使用 8px 宽的描边路径，方便用户点中连线选中/删除。
        8px 是平衡值：太宽会盖住端口附近点击；太窄用户点不中。"""
        stroker = QPainterPathStroker()
        stroker.setWidth(8)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:
        """重写：boundingRect 也要足够大以容纳 8px 宽的命中区域。"""
        pad = 8
        base = self.path().boundingRect()
        return base.adjusted(-pad, -pad, pad, pad)

    def hoverEnterEvent(self, event):
        self._hover = True
        self._update_pen()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self._update_pen()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        # 用 ItemSelectedHasChanged 而非 ItemSelectedChange：
        # 后者在选中状态"即将改变"时触发，此时 isSelected() 还返回旧值，
        # 导致 _update_pen 里 self.isSelected() 取到旧状态，颜色不立即切换
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self._update_pen()
        return super().itemChange(change, value)
