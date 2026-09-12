"""识别参数预设表。

供 src.blueprint.runtime.template_matcher 按 key 取阈值/超时等默认参数。
"""

DEFAULT_RECOGNITION_PROFILES = {
    "match.default":       {"threshold": 0.80, "timeout": 30, "interval": 0.30, "fast_mode": True},
    "match.fast":          {"threshold": 0.72, "timeout": 5,  "interval": 0.15, "fast_mode": True},
    "match.strict":        {"threshold": 0.90, "timeout": 30, "interval": 0.40, "fast_mode": False},
    "match.invert":        {"threshold": 0.70, "timeout": 30, "interval": 0.30, "fast_mode": False, "invert_mode": True},
    "match.element_stable":{"threshold": 0.85, "timeout": 30, "interval": 0.25, "fast_mode": False},
    "combo.default":       {"main_threshold": 0.80, "like_threshold": 0.70, "final_threshold": 0.75, "fast_mode": True},
}
