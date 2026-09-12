"""蓝图导出为独立可执行文件的公共模块。

这里只放【主程序与运行时真正共用】的纯逻辑（不依赖 Qt）：
    - overlay.py : 把载荷追加/读取到 exe 末尾
    - payload.py : 蓝图资源收集、路径记号化、载荷 zip 打包与还原

主程序侧的"找模板 exe + 写文件"逻辑在 exporter.py（运行时不需要它），
导出对话框在 src/blueprint/ui/export_dialog.py。
"""

from .overlay import (
    MAGIC,
    append_payload_to_exe,
    build_overlay,
    has_payload,
    read_payload,
)
from .payload import (
    BLUEPRINT_NAME,
    FORMAT_TAG,
    FORMAT_VERSION,
    IMAGE_EXTS,
    MANIFEST_NAME,
    TOKEN_PREFIX,
    PayloadBuildResult,
    build_payload,
    extract_to,
    load_manifest,
    restore_blueprint_dict,
    scan_assets,
)

__all__ = [
    "MAGIC",
    "append_payload_to_exe",
    "build_overlay",
    "has_payload",
    "read_payload",
    "BLUEPRINT_NAME",
    "MANIFEST_NAME",
    "FORMAT_TAG",
    "FORMAT_VERSION",
    "IMAGE_EXTS",
    "TOKEN_PREFIX",
    "PayloadBuildResult",
    "build_payload",
    "extract_to",
    "load_manifest",
    "restore_blueprint_dict",
    "scan_assets",
]
