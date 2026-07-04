import copy
import logging

_log = logging.getLogger(__name__)


DEFAULT_RECOGNITION_PROFILES = {
    "match.default":       {"threshold": 0.80, "timeout": 30, "interval": 0.30, "fast_mode": True},
    "match.fast":          {"threshold": 0.72, "timeout": 5,  "interval": 0.15, "fast_mode": True},
    "match.strict":        {"threshold": 0.90, "timeout": 30, "interval": 0.40, "fast_mode": False},
    "match.invert":        {"threshold": 0.70, "timeout": 30, "interval": 0.30, "fast_mode": False, "invert_mode": True},
    "match.element_stable":{"threshold": 0.85, "timeout": 30, "interval": 0.25, "fast_mode": False},
    "combo.default":       {"main_threshold": 0.80, "like_threshold": 0.70, "final_threshold": 0.75, "fast_mode": True},
}


def get_recognition_profile(bot, key, **overrides):
    base = copy.deepcopy(DEFAULT_RECOGNITION_PROFILES.get(key, {}))
    user_profiles = getattr(bot, "config", {}).get("recognition_profiles", {}) or {}
    user_profile = user_profiles.get(key, {})
    if isinstance(user_profile, dict):
        base.update(user_profile)
    if overrides:
        base.update({k: v for k, v in overrides.items() if v is not None})
    return base
