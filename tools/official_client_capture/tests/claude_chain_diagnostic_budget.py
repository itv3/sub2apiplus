"""仅供显式诊断入口使用的独立预算；正式公开链拒绝继承该开关。"""

import os

DIAGNOSTIC_TIMEOUT_ENV = "CLAUDE_FAULT_CHAIN_DIAGNOSTIC_SECONDS"


def validate_seconds(value: int) -> int:
    """允许每个独立场景十五分钟到两小时，仍保留挂死清理上限。"""
    if type(value) is not int or not 900 <= value <= 7200:
        raise ValueError("诊断场景预算必须为 900 至 7200 秒的整数")
    return value


def diagnostic_seconds() -> int | None:
    value = os.environ.get(DIAGNOSTIC_TIMEOUT_ENV)
    if value is None:
        return None
    if not value.isdecimal():
        raise ValueError("诊断场景预算不是整数")
    return validate_seconds(int(value))
