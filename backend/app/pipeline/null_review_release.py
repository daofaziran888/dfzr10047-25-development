"""原值空值复核、坏行回补与发布重放的独立管线组件。

与服务层共享同一套裁决引擎，但使用实例私有的内存表，适合不依赖全局 store 的组件测试。
"""
from __future__ import annotations

from typing import Any

from app.services.value_release import (
    MemoryBackend,
    Outcome,
    ValueReleaseEngine,
)


class NullReviewReleaseManager(ValueReleaseEngine):
    """管理合法空值授权、xx→7 修正回执、契约重算和幂等发布重放。"""

    def __init__(self) -> None:
        super().__init__(MemoryBackend())


# 常见命名别名。
OriginalValueReviewManager = NullReviewReleaseManager
ValueNullReviewManager = NullReviewReleaseManager
NullValueReviewManager = NullReviewReleaseManager
ReleaseReplayManager = NullReviewReleaseManager
ValueReleaseReplayManager = NullReviewReleaseManager
NullReviewReleaseOrchestrator = NullReviewReleaseManager
ValueReleaseOrchestrator = NullReviewReleaseManager
OriginalValueReleaseManager = NullReviewReleaseManager
NullValueReviewOrchestrator = NullReviewReleaseManager


__all__ = [
    "NullReviewReleaseManager", "OriginalValueReviewManager",
    "ValueNullReviewManager", "NullValueReviewManager", "ReleaseReplayManager",
    "ValueReleaseReplayManager", "NullReviewReleaseOrchestrator",
    "ValueReleaseOrchestrator", "OriginalValueReleaseManager",
    "NullValueReviewOrchestrator", "Outcome",
]
