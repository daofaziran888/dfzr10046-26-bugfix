"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import flow, resource

# flow 中的固定路径（/late_fact/submissions 等）需要先于
# /api/{module}/{entry_id} 动态路由注册。
ROUTERS = [flow, resource]
