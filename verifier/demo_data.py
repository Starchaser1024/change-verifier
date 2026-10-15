"""演示数据：示例任务、接口清单与一套完整的验收规则（10 条检查）。

预期运行结果（见 tests/test_e2e_demo.py）：
- 通过 2 条：登录、读取自己的订单
- 本次引入的回归 4 条：跨用户读订单、未登录访问列表、普通用户调用管理接口、重复创建不幂等
- 原有问题 1 条：不存在的订单返回 500
- 符合已确认需求的行为变化 1 条：列表结构从 {orders,count} 换成 {items,total}
- 新需求满足 1 条：列表返回 {items,total}
- 新需求未满足 1 条：page_size 参数被忽略
"""

from __future__ import annotations

import copy

DEMO_BASELINE_URL = "http://127.0.0.1:8620"
DEMO_CANDIDATE_URL = "http://127.0.0.1:8621"

DEMO_TASK = {
    "name": "用户与订单服务：改动验收（演示）",
    "requirement": (
        "订单服务本次改动需要满足：\n"
        "1. 「订单列表」接口 GET /api/orders 的响应结构改为 {items, total}（原为 {orders, count}）；\n"
        "2. 「订单列表」接口支持 page / page_size 分页参数，page_size 限制返回条数；\n"
        "3. 除此之外：登录、订单归属权限、管理员权限、创建订单的 Idempotency-Key 幂等性等原有行为必须保持不变；\n"
        "4. 读取不存在的订单目前返回 500，属于已知原有问题，本次不要求修复。"
    ),
    "baseline_url": DEMO_BASELINE_URL,
    "candidate_url": DEMO_CANDIDATE_URL,
}

DEMO_ENDPOINTS = [
    {"method": "POST", "path": "/api/login", "summary": "用户名密码登录，返回 access_token"},
    {"method": "GET", "path": "/api/orders", "summary": "当前用户订单列表（新需求: {items,total} + page/page_size）"},
    {"method": "GET", "path": "/api/orders/{order_id}", "summary": "订单详情（仅限本人订单）"},
    {"method": "POST", "path": "/api/orders", "summary": "创建订单（支持 Idempotency-Key 幂等键）"},
    {"method": "GET", "path": "/api/admin/users", "summary": "管理员查看用户列表（仅管理员）"},
]


def _login_step(assert_status: bool = True) -> dict:
    step = {
        "name": "使用 alice 账号登录",
        "method": "POST",
        "path": "/api/login",
        "json": {"username": "alice", "password": "alice-password"},
        "extract": {"token": {"source": "json", "path": "$.access_token", "sensitive": True}},
        "assert": [{"type": "status", "op": "equals", "value": 200}],
    }
    if not assert_status:
        step["assert"] = [{"type": "status", "op": "in", "value": [200, 201]}]
    return step


def _auth_header() -> dict:
    return {"Authorization": "Bearer {{token}}"}


def demo_checks() -> list[dict]:
    """返回一套全新的演示检查列表（深拷贝，可安全修改）。"""
    checks: list[dict] = [
        {
            "id": "login-success",
            "name": "使用正确密码可以登录并拿到 token",
            "category": "existing_behavior",
            "expectation": "POST /api/login 返回 200、access_token 与 user.id=1（alice）",
            "steps": [
                {
                    **_login_step(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.access_token", "op": "exists"},
                        {"type": "json", "path": "$.user.id", "op": "equals", "value": 1},
                        {"type": "json", "path": "$.user.username", "op": "equals", "value": "alice"},
                    ],
                }
            ],
        },
        {
            "id": "read-own-order",
            "name": "用户能读取自己的订单",
            "category": "existing_behavior",
            "expectation": "alice(ID=1) 读取订单 101 返回 200，且 owner_id=1",
            "steps": [
                _login_step(),
                {
                    "name": "读取自己的订单 101",
                    "method": "GET",
                    "path": "/api/orders/101",
                    "headers": _auth_header(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.id", "op": "equals", "value": 101},
                        {"type": "json", "path": "$.owner_id", "op": "equals", "value": 1},
                    ],
                },
            ],
        },
        {
            "id": "read-other-order-denied",
            "name": "用户 A 不能读取用户 B 的订单",
            "category": "existing_behavior",
            "expectation": "alice 读取 bob 的订单 102 必须返回 403（或 404），不能返回订单内容",
            "steps": [
                _login_step(),
                {
                    "name": "以 alice 身份读取 bob 的订单 102",
                    "method": "GET",
                    "path": "/api/orders/102",
                    "headers": _auth_header(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 403},
                        {"type": "json", "path": "$.owner_id", "op": "not_exists"},
                    ],
                },
            ],
        },
        {
            "id": "anonymous-list-denied",
            "name": "未登录不能访问订单列表",
            "category": "existing_behavior",
            "expectation": "不携带 token 请求 GET /api/orders 必须返回 401",
            "steps": [
                {
                    "name": "匿名请求订单列表",
                    "method": "GET",
                    "path": "/api/orders",
                    "assert": [{"type": "status", "op": "equals", "value": 401}],
                }
            ],
        },
        {
            "id": "admin-endpoint-forbidden",
            "name": "普通用户不能调用管理员接口",
            "category": "existing_behavior",
            "expectation": "alice（role=user）调用 GET /api/admin/users 必须返回 403",
            "steps": [
                _login_step(),
                {
                    "name": "以普通用户身份调用管理接口",
                    "method": "GET",
                    "path": "/api/admin/users",
                    "headers": _auth_header(),
                    "assert": [{"type": "status", "op": "equals", "value": 403}],
                },
            ],
        },
        {
            "id": "create-order-idempotent",
            "name": "带 Idempotency-Key 的重复创建不会生成重复订单",
            "category": "existing_behavior",
            "expectation": "同一 Idempotency-Key 连续 POST /api/orders 两次，第二次必须返回第一次的订单 id",
            "steps": [
                _login_step(),
                {
                    "name": "第一次创建订单（带幂等键）",
                    "method": "POST",
                    "path": "/api/orders",
                    "headers": {**_auth_header(), "Idempotency-Key": "{{nonce}}-idem"},
                    "json": {"item": "widget", "quantity": 1},
                    "extract": {"first_order_id": {"source": "json", "path": "$.id"}},
                    "assert": [{"type": "status", "op": "in", "value": [200, 201]}],
                },
                {
                    "name": "使用同一幂等键重复创建",
                    "method": "POST",
                    "path": "/api/orders",
                    "headers": {**_auth_header(), "Idempotency-Key": "{{nonce}}-idem"},
                    "json": {"item": "widget", "quantity": 1},
                    "assert": [
                        {"type": "status", "op": "in", "value": [200, 201]},
                        {"type": "json", "path": "$.id", "op": "equals", "value": "{{first_order_id}}"},
                    ],
                },
            ],
        },
        {
            "id": "list-legacy-envelope",
            "name": "订单列表沿用 {orders, count} 结构（已被新需求替代）",
            "category": "existing_behavior",
            "expected_change_ref": "list-new-envelope",
            "expectation": "GET /api/orders 返回 orders 数组与 count 数字",
            "steps": [
                _login_step(),
                {
                    "name": "读取订单列表（旧结构）",
                    "method": "GET",
                    "path": "/api/orders",
                    "headers": _auth_header(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.orders", "op": "type", "value": "array"},
                        {"type": "json", "path": "$.count", "op": "type", "value": "number"},
                    ],
                },
            ],
        },
        {
            "id": "missing-order-status",
            "name": "读取不存在的订单应返回 404（已知原有问题）",
            "category": "existing_behavior",
            "expectation": "GET /api/orders/999999 应返回 404，但当前两个版本都返回 500（原有问题）",
            "steps": [
                _login_step(),
                {
                    "name": "读取不存在的订单 999999",
                    "method": "GET",
                    "path": "/api/orders/999999",
                    "headers": _auth_header(),
                    "assert": [{"type": "status", "op": "equals", "value": 404}],
                },
            ],
        },
        {
            "id": "list-new-envelope",
            "name": "订单列表返回 {items, total} 结构（新需求）",
            "category": "new_requirement",
            "expectation": "GET /api/orders 返回 items 数组与 total 数字（total>0）",
            "steps": [
                _login_step(),
                {
                    "name": "读取订单列表（新结构）",
                    "method": "GET",
                    "path": "/api/orders",
                    "headers": _auth_header(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.items", "op": "type", "value": "array"},
                        {"type": "json", "path": "$.total", "op": "type", "value": "number"},
                        {"type": "json", "path": "$.total", "op": "greater_than", "value": 0},
                    ],
                },
            ],
        },
        {
            "id": "list-page-size",
            "name": "订单列表支持 page_size 限制返回条数（新需求）",
            "category": "new_requirement",
            "expectation": "GET /api/orders?page_size=1 只返回 1 条订单（items 长度为 1）",
            "steps": [
                _login_step(),
                {
                    "name": "按 page_size=1 读取订单列表",
                    "method": "GET",
                    "path": "/api/orders?page_size=1",
                    "headers": _auth_header(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.items", "op": "length_equals", "value": 1},
                    ],
                },
            ],
        },
    ]
    return copy.deepcopy(checks)


def demo_rule_note() -> str:
    return "演示规则 v1（由「加载演示规则」按钮生成，需用户确认后使用）"
