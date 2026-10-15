"""演示用「用户与订单」服务。

- baseline: 原有行为正确，作为「修改前」环境。
- candidate: 在同一份代码上人为加入若干明确标注的演示缺陷，作为「修改后」环境。

演示缺陷只存在于本示例服务中，代表 AI 修改代码后常见的错误：
  A. 读取订单时缺少归属校验（跨用户越权读）
  B. 管理接口缺少角色校验（普通用户可调用）
  C. 创建订单忽略 Idempotency-Key（重复请求产生重复订单）
  D. 订单列表缺少登录校验（未登录也能读取）
  E. 忽略 page_size 分页参数（新需求未实现）

缺陷开关（用于变异评估 scripts/mutation_eval.py）：
- candidate 默认开启全部 5 个缺陷，行为与演示一致；
- 环境变量 DEMO_DEFECTS 可指定缺陷子集，例如 DEMO_DEFECTS=A,C 或 DEMO_DEFECTS=none；
- baseline 永远没有缺陷。

两个服务使用完全一致的初始种子数据，并都提供 POST /api/debug/reset 用于恢复初始数据。
原有问题（两个版本都存在的 bug）：读取不存在的订单会触发 500。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------- 种子数据
USERS = {
    1: {"id": 1, "username": "alice", "password": "alice-password", "role": "user"},
    2: {"id": 2, "username": "bob", "password": "bob-password", "role": "user"},
    3: {"id": 3, "username": "admin", "password": "admin-password", "role": "admin"},
}

SEED_ORDERS = {
    101: {"id": 101, "owner_id": 1, "item": "keyboard", "quantity": 1, "status": "created"},
    102: {"id": 102, "owner_id": 2, "item": "monitor", "quantity": 2, "status": "created"},
    103: {"id": 103, "owner_id": 1, "item": "mouse", "quantity": 1, "status": "created"},
    104: {"id": 104, "owner_id": 2, "item": "webcam", "quantity": 1, "status": "created"},
    105: {"id": 105, "owner_id": 1, "item": "usb-cable", "quantity": 3, "status": "created"},
    106: {"id": 106, "owner_id": 2, "item": "headset", "quantity": 1, "status": "created"},
}

DEFECTS_CANDIDATE = [
    {"id": "A", "title": "读取订单时缺少归属校验", "detail": "登录用户可以读取任意用户的订单（跨用户越权读）"},
    {"id": "B", "title": "管理接口缺少角色校验", "detail": "普通用户 token 可以调用 GET /api/admin/users"},
    {"id": "C", "title": "创建订单忽略 Idempotency-Key", "detail": "重复请求使用同一 Idempotency-Key 仍会生成新订单"},
    {"id": "D", "title": "订单列表缺少登录校验", "detail": "未携带 token 也能读取订单列表"},
    {"id": "E", "title": "忽略 page_size 分页参数", "detail": "新需求「订单列表支持 page_size」未实现"},
]

DEFAULT_CANDIDATE_DEFECTS = tuple(d["id"] for d in DEFECTS_CANDIDATE)


def parse_defects(value: Optional[str]) -> Optional[set[str]]:
    """解析 DEMO_DEFECTS 环境变量。

    - None 或空字符串 -> None（用默认值：candidate 全开、baseline 无缺陷）
    - "none" -> 空集合（修好了全部演示缺陷）
    - "A,C" / "a, c" -> {"A", "C"}
    - 其它编号抛 ValueError，避免拼错时静默跑成「无缺陷」。
    """
    if value is None or not value.strip():
        return None
    if value.strip().lower() == "none":
        return set()
    ids: set[str] = set()
    for part in value.replace("，", ",").split(","):
        part = part.strip().upper()
        if not part:
            continue
        if part not in DEFAULT_CANDIDATE_DEFECTS:
            raise ValueError(
                f"未知的演示缺陷编号 {part!r}；可选：{', '.join(DEFAULT_CANDIDATE_DEFECTS)}，或 none"
            )
        ids.add(part)
    return ids


class LoginIn(BaseModel):
    username: str
    password: str


class OrderIn(BaseModel):
    item: str
    quantity: int = 1


def create_app(mode: str, defects: Optional[set[str]] = None) -> FastAPI:
    assert mode in ("baseline", "candidate"), "mode 必须是 baseline 或 candidate"
    if defects is None:
        defects = set(DEFAULT_CANDIDATE_DEFECTS) if mode == "candidate" else set()
    defects = {str(d).strip().upper() for d in defects}

    app = FastAPI(title=f"demo-user-order-{mode}", version="0.1.0")
    state = {
        "orders": {oid: dict(o) for oid, o in SEED_ORDERS.items()},
        "tokens": {},          # token -> user_id
        "next_id": 1000,
        "idempotency": {},     # idempotency_key -> order_id
        "lock": threading.Lock(),
    }

    def user_from_token(authorization: Optional[str]) -> Optional[dict]:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        uid = state["tokens"].get(authorization[len("Bearer "):])
        return USERS.get(uid) if uid is not None else None

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "mode": mode}

    @app.get("/api/meta/defects")
    def meta_defects():
        """列出本服务中当前生效的人为缺陷（仅用于演示说明）。"""
        return {"mode": mode, "defects": [d for d in DEFECTS_CANDIDATE if d["id"] in defects]}

    @app.post("/api/debug/reset")
    def debug_reset():
        """恢复初始种子数据。"""
        with state["lock"]:
            state["orders"] = {oid: dict(o) for oid, o in SEED_ORDERS.items()}
            state["tokens"] = {}
            state["idempotency"] = {}
            state["next_id"] = 1000
        return {"ok": True, "mode": mode}

    @app.get("/api/debug/slow")
    def debug_slow(ms: int = 1000):
        """用于演示超时归类（sleep 指定毫秒后返回）。"""
        time.sleep(min(max(ms, 0), 15000) / 1000.0)
        return {"slept_ms": ms}

    @app.post("/api/login")
    def login(body: LoginIn):
        for u in USERS.values():
            if u["username"] == body.username and u["password"] == body.password:
                token = secrets.token_urlsafe(24)
                state["tokens"][token] = u["id"]
                return {
                    "access_token": token,
                    "token_type": "bearer",
                    "user": {"id": u["id"], "username": u["username"], "role": u["role"]},
                }
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    @app.get("/api/orders")
    def list_orders(
        authorization: Optional[str] = Header(default=None),
        page: int = 1,
        page_size: int = 20,
    ):
        user = user_from_token(authorization)
        if user is None:
            if "D" in defects:
                # DEMO DEFECT D: 删掉了登录校验，未登录时直接按 alice 返回订单
                user = USERS[1]
            else:
                raise HTTPException(status_code=401, detail="未登录")
        with state["lock"]:
            orders = [o for o in state["orders"].values() if o["owner_id"] == user["id"]]
        orders.sort(key=lambda o: o["id"])
        if mode == "candidate":
            # 新需求：返回 {items, total} 并支持 page/page_size（DEMO DEFECT E 开启时忽略分页）
            if "E" in defects:
                items = orders
            else:
                page_no = max(page, 1)
                size = max(page_size, 1)
                items = orders[(page_no - 1) * size : page_no * size]
            return {"items": items, "total": len(orders)}
        return {"orders": orders, "count": len(orders)}

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: int, authorization: Optional[str] = Header(default=None)):
        user = user_from_token(authorization)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录")
        # 原有问题（baseline 与 candidate 都存在）：不存在的订单直接 KeyError -> 500
        order = state["orders"][order_id]
        if "A" in defects:
            # DEMO DEFECT A: 删掉了归属校验，任意登录用户可以读取任意订单
            return order
        if order["owner_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该订单")
        return order

    @app.post("/api/orders", status_code=201)
    def create_order(
        body: OrderIn,
        authorization: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None),
    ):
        user = user_from_token(authorization)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录")
        with state["lock"]:
            if "C" not in defects and idempotency_key:
                existing = state["idempotency"].get(idempotency_key)
                if existing is not None:
                    return state["orders"][existing]
            # DEMO DEFECT C（开启时）: 完全忽略 Idempotency-Key，重复请求会重复创建
            oid = state["next_id"]
            state["next_id"] += 1
            order = {
                "id": oid,
                "owner_id": user["id"],
                "item": body.item,
                "quantity": body.quantity,
                "status": "created",
            }
            state["orders"][oid] = order
            if idempotency_key:
                state["idempotency"][idempotency_key] = oid
            return order

    @app.get("/api/admin/users")
    def admin_users(authorization: Optional[str] = Header(default=None)):
        user = user_from_token(authorization)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录")
        if "B" not in defects and user["role"] != "admin":
            # DEMO DEFECT B（开启时）: 删掉角色校验，普通用户也能调用管理接口
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return {
            "users": [
                {"id": u["id"], "username": u["username"], "role": u["role"]}
                for u in USERS.values()
            ]
        }

    return app
