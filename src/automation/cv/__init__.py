"""CV 工具包：UI 元素切分与候选框生成（ElementSplitDialog 使用）。

历史遗留的 template_matcher / capture / input 子模块已移除；
运行时找图统一走 src.blueprint.runtime.template_matcher。
"""

from .splitter_integration import (
    run_splitter_on_image_bgr,
    run_splitter_on_region,
    import_element_as_template,
    list_elements_in_dir,
    build_candidate_boxes_from_image,
)

__all__ = [
    "run_splitter_on_image_bgr",
    "run_splitter_on_region",
    "import_element_as_template",
    "list_elements_in_dir",
    "build_candidate_boxes_from_image",
]
