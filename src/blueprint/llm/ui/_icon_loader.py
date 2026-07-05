# -*- coding: utf-8 -*-
"""赫萝 Icon 资源统一加载 + 缓存（避免每次 QPixmap 解码大图 1.8MB）。"""
from __future__ import annotations

import os
from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPainterPath, QPen, QColor

_ICON_DIR = os.path.normpath(os.path.join(
    os.path.abspath(os.path.dirname(__file__)),
    "..", "..", "..", "..", "images", "llm_icons",
))

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
