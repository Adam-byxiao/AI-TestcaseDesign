"""领域 Profile：提供默认模块与覆盖阈值，不限制动态模块发现。"""
from __future__ import annotations

PROFILES = {
    "generic": {"modules": ["COMMON", "PERF"], "minCasesPerRequirement": 1},
    "ecommerce": {
        "modules": ["AUTH", "HOME", "PROD", "SEARCH", "CART", "CHECKOUT", "COUPON",
                    "ORDER", "ACCOUNT", "COMMENT", "COMMON", "PERF"],
        "minCasesPerRequirement": 1,
    },
}


def get_profile(name: str) -> dict:
    if name not in PROFILES:
        raise ValueError(f"未知 profile: {name}；可用: {', '.join(sorted(PROFILES))}")
    return PROFILES[name]

