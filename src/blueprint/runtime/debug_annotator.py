import os
import re
import threading
import time

import cv2

from ...config.app_paths import debug_dir


# 步骤号自增锁：并行分支是多线程并发，`ctx.step_counter += 1` 不是原子操作，
# 两个线程可能读到同一个值再各自写回 → 撞号（两张证据图写进同一文件名互相覆盖），
# 日志里的 Step 号也会乱序。统一走这把锁自增。
_step_counter_lock = threading.Lock()


def bump_step(ctx) -> int:
    """原子地推进并返回当前步骤号（所有 step_counter 自增都必须走这里）。"""
    with _step_counter_lock:
        ctx.step_counter += 1
        return ctx.step_counter


def _safe_dirs(run_id: str) -> str:
    out_dir = os.path.join(debug_dir(), "runs", run_id)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def annotate_step(ctx, node, kind, pos=None, region=None, template_rect=None, note=""):
    """
    硬约束 §1：动作节点后自动截图标注并保存。

    kind: {"find", "click", "keypress", "wait", "branch", "loop", "target", "var", "edge"}
    pos:    (x,y)  蓝(找图)或红(点击)十字+圆+标签
    region: (x,y,w,h) 绿框+标签
    template_rect: (x,y,w,h) 找图命中的模板矩形(蓝框)
    note:   顶部白字备注
    """
    step = bump_step(ctx)
    try:
        screen = None
        if ctx.screen is not None:
            screen = ctx.screen.capture_full_bgr()
        if screen is None:
            return
        H, W = screen.shape[:2]

        if region and len(region) == 4:
            x, y, w, h = map(int, region)
            x = max(0, min(x, W - 1))
            y = max(0, min(y, H - 1))
            w = max(1, min(w, W - x))
            h = max(1, min(h, H - y))
            cv2.rectangle(screen, (x, y), (x + w, y + h), (0, 200, 0), 2)
            cv2.putText(
                screen, f"ROI({x},{y},{w},{h})",
                (x, max(12, y - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1, cv2.LINE_AA,
            )

        if template_rect and len(template_rect) == 4:
            x, y, w, h = map(int, template_rect)
            cv2.rectangle(screen, (x, y), (x + w, y + h), (220, 160, 0), 2)
            cv2.putText(
                screen, "TPL",
                (x, max(12, y - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 170, 20), 1, cv2.LINE_AA,
            )

        if pos and len(pos) == 2:
            px, py = map(int, pos)
            px = max(0, min(px, W - 1))
            py = max(0, min(py, H - 1))
            if kind == "click":
                color = (0, 0, 255)
            elif kind == "find":
                color = (230, 130, 0)
            else:
                color = (255, 200, 50)
            cv2.drawMarker(screen, (px, py), color, cv2.MARKER_CROSS, 26, 2)
            cv2.circle(screen, (px, py), 7, color, -1)
            label = f"{kind}({px},{py})"
            cv2.putText(
                screen, label,
                (px + 10, py - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA,
            )

        if note:
            cv2.rectangle(screen, (0, 0), (min(W, 900), 36), (0, 0, 0), -1)
            cv2.putText(
                screen, note[:140],
                (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA,
            )

        footer = f"Step {step:04d} | {node.type}/{kind} | {time.strftime('%H:%M:%S')}"
        cv2.rectangle(screen, (0, max(0, H - 28)), (W, H), (0, 0, 0), -1)
        cv2.putText(
            screen, footer,
            (10, H - 8),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (210, 210, 210), 1, cv2.LINE_AA,
        )

        safe_type = re.sub(r"[^\w]", "_", str(node.type))
        safe_kind = re.sub(r"[^\w]", "_", str(kind))
        out_dir = _safe_dirs(ctx.run_id)
        filename = f"step_{step:04d}_{safe_type}_{safe_kind}.png"
        path = os.path.join(out_dir, filename)
        ok, buf = cv2.imencode(".png", screen)
        if ok:
            buf.tofile(path)
        ctx.log(
            f"[Step {step:04d}] {node.type}/{kind} 证据: .../{filename} | {note}",
            "DEBUG",
        )
    except Exception as e:
        try:
            ctx.log(f"[Step {step:04d}] annotate 失败: {e}", "WARN")
        except Exception:
            pass
