"""发布申请服务的兼容导入路径，领域实现见 value_release。"""
from app.services.value_release import (  # noqa: F401
    PublishRequestService,
    ReleaseRequestService,
    ValueReleaseRequestService,
    ValueReleaseService,
    engine,
)
