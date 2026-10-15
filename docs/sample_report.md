# 验收报告：用户与订单服务：改动验收（演示）

- 生成时间：2026-10-15T16:36:49+08:00
- 任务 ID：1；运行 ID：1；规则版本：v1（已确认）
- 规则说明：演示规则 v1（由「加载演示规则」按钮生成，需用户确认后使用）
- 运行状态：completed；开始：2026-10-15T16:36:25+08:00；结束：2026-10-15T16:36:40+08:00
- 修改前环境：`http://127.0.0.1:8620`
- 修改后环境：`http://127.0.0.1:8621`

## 需求

订单服务本次改动需要满足：
1. 「订单列表」接口 GET /api/orders 的响应结构改为 {items, total}（原为 {orders, count}）；
2. 「订单列表」接口支持 page / page_size 分页参数，page_size 限制返回条数；
3. 除此之外：登录、订单归属权限、管理员权限、创建订单的 Idempotency-Key 幂等性等原有行为必须保持不变；
4. 读取不存在的订单目前返回 500，属于已知原有问题，本次不要求修复。

## 结论

**发现 4 项本次引入的回归，建议修复后再上线**
- 另有 1 项新需求未满足
- 1 项为原有问题（修改前后都失败）
- 1 项行为变化已由确认的新需求解释

## 结果汇总

| 分类 | 数量 |
| --- | --- |
| 本次引入的回归 | 4 |
| 新需求未满足 | 1 |
| 原有问题 | 1 |
| 通过 | 2 |
| 新需求满足 | 1 |
| 符合已确认需求的行为变化 | 1 |
| **合计** | **10 项检查（已执行 10 项）** |

## 检查明细

### 1. [通过] 使用正确密码可以登录并拿到 token

- 检查 id：`login-success`；类型：保持原有行为
- 期望：POST /api/login 返回 200、access_token 与 user.id=1（alice）
- 判定：**通过** —— 修改前后均通过
- 前后对比：修改前：通过；修改后：通过

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（3 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✓ 断言：响应 JSON $.access_token exists（期望 null，实际 "q7VYoOuURlgx3QCI03_Dv1Pr7XMAt9oC"）
    - ✓ 断言：响应 JSON $.user.id equals 1（期望 1，实际 1）
    - ✓ 断言：响应 JSON $.user.username equals alice（期望 "alice"，实际 "alice"）

**修改后（candidate）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（19 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✓ 断言：响应 JSON $.access_token exists（期望 null，实际 "ddKwJtcnFOFWzD82iVLncbGgqckrq7Nz"）
    - ✓ 断言：响应 JSON $.user.id equals 1（期望 1，实际 1）
    - ✓ 断言：响应 JSON $.user.username equals alice（期望 "alice"，实际 "alice"）

---

### 2. [通过] 用户能读取自己的订单

- 检查 id：`read-own-order`；类型：保持原有行为
- 期望：alice(ID=1) 读取订单 101 返回 200，且 owner_id=1
- 判定：**通过** —— 修改前后均通过
- 前后对比：修改前：通过；修改后：通过

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（25 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取自己的订单 101」
  - 请求：`GET /api/orders/101`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "id": 101,
        "owner_id": 1,
        "item": "keyboard",
        "quantity": 1,
        "status": "created"
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✓ 断言：响应 JSON $.id equals 101（期望 101，实际 101）
    - ✓ 断言：响应 JSON $.owner_id equals 1（期望 1，实际 1）

**修改后（candidate）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（18 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取自己的订单 101」
  - 请求：`GET /api/orders/101`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "id": 101,
        "owner_id": 1,
        "item": "keyboard",
        "quantity": 1,
        "status": "created"
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✓ 断言：响应 JSON $.id equals 101（期望 101，实际 101）
    - ✓ 断言：响应 JSON $.owner_id equals 1（期望 1，实际 1）

---

### 3. [本次引入的回归] 用户 A 不能读取用户 B 的订单

- 检查 id：`read-other-order-denied`；类型：保持原有行为
- 期望：alice 读取 bob 的订单 102 必须返回 403（或 404），不能返回订单内容
- 判定：**本次引入的回归** —— 修改前通过，修改后失败：步骤「以 alice 身份读取 bob 的订单 102」断言失败：状态码 equals 403（期望 403，实际 200）
- 前后对比：修改前：通过；修改后：失败（变化：通过 -> 失败）

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「以 alice 身份读取 bob 的订单 102」
  - 请求：`GET /api/orders/102`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`403`（1 ms）
      ```json
      {
        "detail": "无权访问该订单"
      }
      ```
    - ✓ 断言：状态码 equals 403（期望 403，实际 403）
    - ✓ 断言：响应 JSON $.owner_id not_exists（期望 null，实际 <字段不存在>）

**修改后（candidate）**：失败 —— 步骤「以 alice 身份读取 bob 的订单 102」断言失败：状态码 equals 403（期望 403，实际 200）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（2 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「以 alice 身份读取 bob 的订单 102」
  - 请求：`GET /api/orders/102`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（0 ms）
      ```json
      {
        "id": 102,
        "owner_id": 2,
        "item": "monitor",
        "quantity": 2,
        "status": "created"
      }
      ```
    - ✗ 断言：状态码 equals 403（期望 403，实际 200）
    - ✗ 断言：响应 JSON $.owner_id not_exists（期望 null，实际 2）

---

### 4. [本次引入的回归] 未登录不能访问订单列表

- 检查 id：`anonymous-list-denied`；类型：保持原有行为
- 期望：不携带 token 请求 GET /api/orders 必须返回 401
- 判定：**本次引入的回归** —— 修改前通过，修改后失败：步骤「匿名请求订单列表」断言失败：状态码 equals 401（期望 401，实际 200）
- 前后对比：修改前：通过；修改后：失败（变化：通过 -> 失败）

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「匿名请求订单列表」
  - 请求：`GET /api/orders`
    - 响应：`401`（18 ms）
      ```json
      {
        "detail": "未登录"
      }
      ```
    - ✓ 断言：状态码 equals 401（期望 401，实际 401）

**修改后（candidate）**：失败 —— 步骤「匿名请求订单列表」断言失败：状态码 equals 401（期望 401，实际 200）
- 步骤「匿名请求订单列表」
  - 请求：`GET /api/orders`
    - 响应：`200`（2 ms）
      ```json
      {
        "items": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          }
        ],
        "total": 3
      }
      ```
    - ✗ 断言：状态码 equals 401（期望 401，实际 200）

---

### 5. [本次引入的回归] 普通用户不能调用管理员接口

- 检查 id：`admin-endpoint-forbidden`；类型：保持原有行为
- 期望：alice（role=user）调用 GET /api/admin/users 必须返回 403
- 判定：**本次引入的回归** —— 修改前通过，修改后失败：步骤「以普通用户身份调用管理接口」断言失败：状态码 equals 403（期望 403，实际 200）
- 前后对比：修改前：通过；修改后：失败（变化：通过 -> 失败）

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（19 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「以普通用户身份调用管理接口」
  - 请求：`GET /api/admin/users`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`403`（2 ms）
      ```json
      {
        "detail": "需要管理员权限"
      }
      ```
    - ✓ 断言：状态码 equals 403（期望 403，实际 403）

**修改后（candidate）**：失败 —— 步骤「以普通用户身份调用管理接口」断言失败：状态码 equals 403（期望 403，实际 200）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（2 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「以普通用户身份调用管理接口」
  - 请求：`GET /api/admin/users`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "users": [
          {
            "id": 1,
            "username": "alice",
            "role": "user"
          },
          {
            "id": 2,
            "username": "bob",
            "role": "user"
          },
          {
            "id": 3,
            "username": "admin",
            "role": "admin"
          }
        ]
      }
      ```
    - ✗ 断言：状态码 equals 403（期望 403，实际 200）

---

### 6. [本次引入的回归] 带 Idempotency-Key 的重复创建不会生成重复订单

- 检查 id：`create-order-idempotent`；类型：保持原有行为
- 期望：同一 Idempotency-Key 连续 POST /api/orders 两次，第二次必须返回第一次的订单 id
- 判定：**本次引入的回归** —— 修改前通过，修改后失败：步骤「使用同一幂等键重复创建」断言失败：响应 JSON $.id equals 1000（期望 1000，实际 1001）
- 前后对比：修改前：通过；修改后：失败（变化：通过 -> 失败）

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（2 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「第一次创建订单（带幂等键）」
  - 请求：`POST /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]", "Idempotency-Key": "b4eaa677-5-idem"}`
    - 请求体：
      ```json
      {
        "item": "widget",
        "quantity": 1
      }
      ```
    - 提取变量：`{"first_order_id": "[REDACTED]"}`
    - 响应：`201`（1 ms）
      ```json
      {
        "id": 1000,
        "owner_id": 1,
        "item": "widget",
        "quantity": 1,
        "status": "created"
      }
      ```
    - ✓ 断言：状态码 in [200, 201]（期望 [200, 201]，实际 201）
- 步骤「使用同一幂等键重复创建」
  - 请求：`POST /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]", "Idempotency-Key": "b4eaa677-5-idem"}`
    - 请求体：
      ```json
      {
        "item": "widget",
        "quantity": 1
      }
      ```
    - 响应：`201`（1 ms）
      ```json
      {
        "id": 1000,
        "owner_id": 1,
        "item": "widget",
        "quantity": 1,
        "status": "created"
      }
      ```
    - ✓ 断言：状态码 in [200, 201]（期望 [200, 201]，实际 201）
    - ✓ 断言：响应 JSON $.id equals 1000（期望 1000，实际 1000）

**修改后（candidate）**：失败 —— 步骤「使用同一幂等键重复创建」断言失败：响应 JSON $.id equals 1000（期望 1000，实际 1001）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（24 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「第一次创建订单（带幂等键）」
  - 请求：`POST /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]", "Idempotency-Key": "b4eaa677-5-idem"}`
    - 请求体：
      ```json
      {
        "item": "widget",
        "quantity": 1
      }
      ```
    - 提取变量：`{"first_order_id": "[REDACTED]"}`
    - 响应：`201`（1 ms）
      ```json
      {
        "id": 1000,
        "owner_id": 1,
        "item": "widget",
        "quantity": 1,
        "status": "created"
      }
      ```
    - ✓ 断言：状态码 in [200, 201]（期望 [200, 201]，实际 201）
- 步骤「使用同一幂等键重复创建」
  - 请求：`POST /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]", "Idempotency-Key": "b4eaa677-5-idem"}`
    - 请求体：
      ```json
      {
        "item": "widget",
        "quantity": 1
      }
      ```
    - 响应：`201`（1 ms）
      ```json
      {
        "id": 1001,
        "owner_id": 1,
        "item": "widget",
        "quantity": 1,
        "status": "created"
      }
      ```
    - ✓ 断言：状态码 in [200, 201]（期望 [200, 201]，实际 201）
    - ✗ 断言：响应 JSON $.id equals 1000（期望 1000，实际 1001）

---

### 7. [符合已确认需求的行为变化] 订单列表沿用 {orders, count} 结构（已被新需求替代）

- 检查 id：`list-legacy-envelope`；类型：保持原有行为
- 期望：GET /api/orders 返回 orders 数组与 count 数字
- 行为变化引用：`list-new-envelope`（修改未回归的依据）
- 判定：**符合已确认需求的行为变化** —— 修改前行为在修改后被改变，但已被已确认的新需求检查「list-new-envelope」覆盖，不算回归
- 前后对比：修改前：通过；修改后：失败（变化：通过 -> 失败）

**修改前（baseline）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（21 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取订单列表（旧结构）」
  - 请求：`GET /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（0 ms）
      ```json
      {
        "orders": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          },
          {
            "id": 1000,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          }
        ],
        "count": 4
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✓ 断言：响应 JSON $.orders type array（期望 "array"，实际 [{"id": 101, "owner_id": 1, "item": "keyboard", "quantity": 1, "status": "created"}, {"id": 103, "owner_id": 1, "item": "mouse", "quantity": 1, "status": "created"}, {"id": 105, "owner_id": 1, "item": "usb-cable", "quantity": 3, "status": "created"}, {"id": 1000, "owner_id": 1, "item": "widget", "qu…）
    - ✓ 断言：响应 JSON $.count type number（期望 "number"，实际 4）

**修改后（candidate）**：失败 —— 步骤「读取订单列表（旧结构）」断言失败：响应 JSON $.orders type array（期望 "array"，实际 <字段不存在>）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取订单列表（旧结构）」
  - 请求：`GET /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（0 ms）
      ```json
      {
        "items": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          },
          {
            "id": 1000,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 1001,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          }
        ],
        "total": 5
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✗ 断言：响应 JSON $.orders type array（期望 "array"，实际 <字段不存在>）
    - ✗ 断言：响应 JSON $.count type number（期望 "number"，实际 <字段不存在>）

---

### 8. [原有问题] 读取不存在的订单应返回 404（已知原有问题）

- 检查 id：`missing-order-status`；类型：保持原有行为
- 期望：GET /api/orders/999999 应返回 404，但当前两个版本都返回 500（原有问题）
- 判定：**原有问题** —— 修改前后均失败（原有问题，非本次引入）
- 前后对比：修改前：失败；修改后：失败

**修改前（baseline）**：失败 —— 步骤「读取不存在的订单 999999」断言失败：状态码 equals 404（期望 404，实际 500）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（14 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取不存在的订单 999999」
  - 请求：`GET /api/orders/999999`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`500`（7 ms）
      ```text
      Internal Server Error
      ```
    - ✗ 断言：状态码 equals 404（期望 404，实际 500）

**修改后（candidate）**：失败 —— 步骤「读取不存在的订单 999999」断言失败：状态码 equals 404（期望 404，实际 500）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（20 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取不存在的订单 999999」
  - 请求：`GET /api/orders/999999`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`500`（7 ms）
      ```text
      Internal Server Error
      ```
    - ✗ 断言：状态码 equals 404（期望 404，实际 500）

---

### 9. [新需求满足] 订单列表返回 {items, total} 结构（新需求）

- 检查 id：`list-new-envelope`；类型：新需求验收
- 期望：GET /api/orders 返回 items 数组与 total 数字（total>0）
- 判定：**新需求满足** —— 修改后满足该新需求（修改前不满足，符合预期）
- 前后对比：修改前：失败；修改后：通过（变化：失败 -> 通过）

**修改前（baseline）**：失败 —— 步骤「读取订单列表（新结构）」断言失败：响应 JSON $.items type array（期望 "array"，实际 <字段不存在>）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（2 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取订单列表（新结构）」
  - 请求：`GET /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "orders": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          },
          {
            "id": 1000,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          }
        ],
        "count": 4
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✗ 断言：响应 JSON $.items type array（期望 "array"，实际 <字段不存在>）
    - ✗ 断言：响应 JSON $.total type number（期望 "number"，实际 <字段不存在>）
    - ✗ 断言：响应 JSON $.total greater_than 0（期望 0，实际 <字段不存在>）

**修改后（candidate）**：通过 —— 全部步骤与断言通过
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（14 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「读取订单列表（新结构）」
  - 请求：`GET /api/orders`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（0 ms）
      ```json
      {
        "items": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          },
          {
            "id": 1000,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 1001,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          }
        ],
        "total": 5
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✓ 断言：响应 JSON $.items type array（期望 "array"，实际 [{"id": 101, "owner_id": 1, "item": "keyboard", "quantity": 1, "status": "created"}, {"id": 103, "owner_id": 1, "item": "mouse", "quantity": 1, "status": "created"}, {"id": 105, "owner_id": 1, "item": "usb-cable", "quantity": 3, "status": "created"}, {"id": 1000, "owner_id": 1, "item": "widget", "qu…）
    - ✓ 断言：响应 JSON $.total type number（期望 "number"，实际 5）
    - ✓ 断言：响应 JSON $.total greater_than 0（期望 0，实际 5）

---

### 10. [新需求未满足] 订单列表支持 page_size 限制返回条数（新需求）

- 检查 id：`list-page-size`；类型：新需求验收
- 期望：GET /api/orders?page_size=1 只返回 1 条订单（items 长度为 1）
- 判定：**新需求未满足** —— 修改后仍未满足该新需求：步骤「按 page_size=1 读取订单列表」断言失败：响应 JSON $.items length_equals 1（期望 1，实际 [{"id": 101, "owner_id": 1, "item": "keyboard", "quantity": 1, "status": "created"}, {"id": 103, "owner_id": 1, "item": "mouse", "quantity": 1, "status": "created"}, {"id": 105, "owner_id": 1, "item": "usb-cable", "quantity": 3, "status": "created"}, {"id": 1000, "owner_id": 1, "item": "widget", "qu…）
- 前后对比：修改前：失败；修改后：失败

**修改前（baseline）**：失败 —— 步骤「按 page_size=1 读取订单列表」断言失败：响应 JSON $.items length_equals 1（期望 1，实际 <字段不存在>）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（3 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「按 page_size=1 读取订单列表」
  - 请求：`GET /api/orders?page_size=1`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "orders": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          },
          {
            "id": 1000,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          }
        ],
        "count": 4
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✗ 断言：响应 JSON $.items length_equals 1（期望 1，实际 <字段不存在>）

**修改后（candidate）**：失败 —— 步骤「按 page_size=1 读取订单列表」断言失败：响应 JSON $.items length_equals 1（期望 1，实际 [{"id": 101, "owner_id": 1, "item": "keyboard", "quantity": 1, "status": "created"}, {"id": 103, "owner_id": 1, "item": "mouse", "quantity": 1, "status": "created"}, {"id": 105, "owner_id": 1, "item": "usb-cable", "quantity": 3, "status": "created"}, {"id": 1000, "owner_id": 1, "item": "widget", "qu…）
- 步骤「使用 alice 账号登录」
  - 请求：`POST /api/login`
    - 请求体：
      ```json
      {
        "username": "alice",
        "password": "[REDACTED]"
      }
      ```
    - 提取变量：`{"token": "[REDACTED]"}`
    - 响应：`200`（11 ms）
      ```json
      {
        "access_token": "[REDACTED]",
        "token_type": "[REDACTED]",
        "user": {
          "id": 1,
          "username": "alice",
          "role": "user"
        }
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
- 步骤「按 page_size=1 读取订单列表」
  - 请求：`GET /api/orders?page_size=1`
    - 请求头：`{"Authorization": "[REDACTED]"}`
    - 响应：`200`（1 ms）
      ```json
      {
        "items": [
          {
            "id": 101,
            "owner_id": 1,
            "item": "keyboard",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 103,
            "owner_id": 1,
            "item": "mouse",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 105,
            "owner_id": 1,
            "item": "usb-cable",
            "quantity": 3,
            "status": "created"
          },
          {
            "id": 1000,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          },
          {
            "id": 1001,
            "owner_id": 1,
            "item": "widget",
            "quantity": 1,
            "status": "created"
          }
        ],
        "total": 5
      }
      ```
    - ✓ 断言：状态码 equals 200（期望 200，实际 200）
    - ✗ 断言：响应 JSON $.items length_equals 1（期望 1，实际 [{"id": 101, "owner_id": 1, "item": "keyboard", "quantity": 1, "status": "created"}, {"id": 103, "owner_id": 1, "item": "mouse", "quantity": 1, "status": "created"}, {"id": 105, "owner_id": 1, "item": "usb-cable", "quantity": 3, "status": "created"}, {"id": 1000, "owner_id": 1, "item": "widget", "qu…）

---

## 范围与限制

- 本报告只覆盖上方列出的检查项，不代表整个项目正确，也不代表不存在安全漏洞。
- 超时、连接失败等环境问题归类为「无法判断」，不视为业务失败。
- 证据为脱敏后的真实请求与响应；敏感字段（Authorization/Cookie/密码/token）已被替换。
- 报告中的结论只对上面列出的检查项负责；未列出的功能与风险未被验证。
