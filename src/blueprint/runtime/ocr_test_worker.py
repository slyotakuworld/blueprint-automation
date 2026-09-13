"""OCR 测试识别子进程 worker。

「测试识别」按钮不能在编辑器 GUI 主进程里直接 import paddle：
paddle 的原生库与 PySide6 的 DLL 同进程加载可能直接崩溃（原生崩溃无法被
try/except 捕获，整个编辑器进程直接消失），且首次初始化引擎可能需要
1~2 分钟，主线程等待会卡死 UI。

所以编辑器把截图写到临时 PNG，用独立子进程跑 OCR，结果以 JSON 写入
指定的结果文件；子进程崩溃也不影响主程序，主程序还能捕获退出码给出提示。

调用方式：
  开发模式: python ocr_test_worker.py <img_path> <lang> <result_path>
  frozen  : BlueprintAutomation.exe --bp-ocr-worker <img_path> <lang> <result_path>
            （blueprint_app.main() 顶部会拦截该参数进入本模块，不启动 GUI）

结果文件 JSON 结构与 ocr_engine.recognize 返回一致：
  {"items": [{"text","conf","box","index"}], "text": str, "sha": str, "error": str|缺失}
"""
import json
import os
import sys


def _setup_import_path():
    """开发模式下把工程根加进 sys.path（本文件位于 <工程根>/src/blueprint/runtime/）。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    if root not in sys.path:
        sys.path.insert(0, root)


def run(img_path: str, lang: str) -> dict:
    """读图（中文路径安全）→ OCR → 返回结果 dict。"""
    import numpy as np
    import cv2

    data = np.fromfile(img_path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return {"items": [], "text": "", "sha": "", "error": "读取临时图像失败"}
    _setup_import_path()
    from src.blueprint.runtime.ocr_engine import recognize
    return recognize(img, lang=lang)


def main(argv):
    img_path = argv[0] if len(argv) > 0 else ""
    lang = argv[1] if len(argv) > 1 else "ch"
    result_path = argv[2] if len(argv) > 2 else ""
    try:
        result = run(img_path, lang)
    except Exception as e:  # 任何异常都落成 JSON，让主程序能显示原因
        import traceback
        result = {"items": [], "text": "", "sha": "",
                  "error": f"{type(e).__name__}: {e}\n---- traceback ----\n{traceback.format_exc()}"}
    if result_path:
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass


if __name__ == "__main__":
    main(sys.argv[1:])
