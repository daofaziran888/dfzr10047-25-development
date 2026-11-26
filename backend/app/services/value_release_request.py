"""发布申请服务的资源名兼容导入路径。"""
from app.services.release_request import (  # noqa: F401
    ReleaseRequestService,
    ValueReleaseRequestService,
    ValueReleaseService,
    engine,
)
