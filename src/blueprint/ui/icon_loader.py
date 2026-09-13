# -*- coding: utf-8 -*-
"""蓝图编辑器通用图标资源统一加载 + 缓存（避免每次 QPixmap 重复解码大图）。

图标源目录：assets/doc_icons/（兼容旧布局 images/doc_icons/）
（打包后为 INTERNAL_DIR/assets/doc_icons，开发模式为源码根 assets/doc_icons）
"""
from __future__ import annotations

import os
from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPainterPath, QPen, QColor


def _resolve_icon_dir() -> str:
    try:
        from src.config.app_paths import INTERNAL_DIR
        for rel in (("assets", "doc_icons"), ("images", "doc_icons")):
            p = os.path.normpath(os.path.join(INTERNAL_DIR, *rel))
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    # 源码模式：本文件在 src/blueprint/ui/ 下，向上 3 级即项目根
    root = os.path.normpath(os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "..", "..", "..",
    ))
    for rel in (("assets", "doc_icons"), ("images", "doc_icons")):
        p = os.path.normpath(os.path.join(root, *rel))
        if os.path.isdir(p):
            return p
    return os.path.normpath(os.path.join(root, "assets", "doc_icons"))


_ICON_DIR = _resolve_icon_dir()


def icon_path(filename: str) -> str:
    return os.path.join(_ICON_DIR, filename).replace("\\", "/")


@lru_cache(maxsize=64)
def _load_raw(filename: str) -> QPixmap:
    pm = QPixmap(icon_path(filename))
    return QPixmap() if pm.isNull() else pm


def pixmap(filename: str, size: int = 32) -> QPixmap:
    raw = _load_raw(filename)
    if raw.isNull():
        return QPixmap()
    return raw.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def icon(filename: str, size: int = 24) -> QIcon:
    pm = pixmap(filename, size)
    return QIcon() if pm.isNull() else QIcon(pm)


def circular_pixmap(filename: str, size: int = 32) -> QPixmap:
    """裁成圆形并描边（用于头像类图标）。"""
    raw = pixmap(filename, size)
    if raw.isNull():
        return QPixmap()
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    p.setClipPath(path)
    p.drawPixmap(0, 0, raw)
    p.setClipping(False)
    p.setPen(QPen(QColor("#F2D4B0"), 1))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(0, 0, size - 1, size - 1)
    p.end()
    return out
