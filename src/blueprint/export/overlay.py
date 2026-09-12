"""OVERLAY 载荷读写：把一段二进制载荷追加到 exe 末尾，仍然保持"单个可执行文件"。

文件布局（从前往后）：

    [ 原始 exe 的全部字节 ]
    [ payload（本项目里是一个 zip 包的字节） ]
    [ 8 字节小端 uint64：payload 的长度 ]
    [ 16 字节固定魔数 ]

读取时从文件末尾倒推：先比魔数，再读长度，就能定位 payload 的起止位置。
PyInstaller 的 onefile 引导程序是从【文件开头】按 cookie 定位它自己的归档的，
末尾追加的数据不会被它读到，所以这么附加是安全的（运行时读的是 sys.executable）。
"""

from __future__ import annotations

import os
from typing import Optional

# 16 字节固定魔数（不要随意改动，改了旧导出物就认不出来了）
MAGIC: bytes = b"BPRUNTIME_PAYLv1"

_LEN_SIZE = 8
TAIL_SIZE = _LEN_SIZE + len(MAGIC)  # 8 + 16 = 24


def build_overlay(exe_bytes: bytes, payload: bytes) -> bytes:
    """把 exe 字节与 payload 拼成最终的导出物字节。"""
    return exe_bytes + payload + len(payload).to_bytes(_LEN_SIZE, "little") + MAGIC


def append_payload_to_exe(src_exe: str, payload: bytes, out_path: str) -> int:
    """流式地把 src_exe 复制到 out_path，并在末尾追加 payload。

    返回写出的总字节数。用分块复制，避免把几十上百 MB 的 exe 一次性读进内存。
    """
    if not os.path.isfile(src_exe):
        raise FileNotFoundError(f"运行时模板不存在：{src_exe}")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    total = 0
    with open(src_exe, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                break
            fout.write(chunk)
            total += len(chunk)
        fout.write(payload)
        total += len(payload)
        fout.write(len(payload).to_bytes(_LEN_SIZE, "little"))
        fout.write(MAGIC)
        total += TAIL_SIZE
    return total


def has_payload(path: str) -> bool:
    """判断某个文件末尾是否带本项目的 OVERLAY 载荷。"""
    try:
        size = os.path.getsize(path)
        if size < TAIL_SIZE:
            return False
        with open(path, "rb") as f:
            f.seek(size - len(MAGIC))
            return f.read(len(MAGIC)) == MAGIC
    except Exception:
        return False


def read_payload(path: str) -> Optional[bytes]:
    """读取文件末尾的 OVERLAY 载荷；没有则返回 None。"""
    try:
        size = os.path.getsize(path)
        if size < TAIL_SIZE:
            return None
        with open(path, "rb") as f:
            f.seek(size - len(MAGIC))
            if f.read(len(MAGIC)) != MAGIC:
                return None
            f.seek(size - TAIL_SIZE)
            raw_len = f.read(_LEN_SIZE)
            payload_len = int.from_bytes(raw_len, "little")
            start = size - TAIL_SIZE - payload_len
            if payload_len <= 0 or start < 0:
                return None
            f.seek(start)
            data = f.read(payload_len)
            if len(data) != payload_len:
                return None
            return data
    except Exception:
        return None
