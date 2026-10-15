# 改动验收器（Change Acceptance Verifier）MVP

用 AI 改完代码后，回答两个问题：**需求真的实现了吗？原有功能被改坏了吗？**

做法：先与用户确认一组**验收规则**（保持原有行为 / 新需求验收），然后在「修改前（baseline）」
和「修改后（candidate）」两个 HTTP 环境上运行**完全相同**的检查，输出分类结论和脱敏后的
请求 / 响应证据。第一版只做 HTTP API 验收，不做浏览器操作。

## 核心原则（已落到实现里）

1. 验收依据 = 用户需求 + 明确接口约定 + **经用户确认**的规则版本（规则按版本存 SQLite，只增不改）。
2. 运行**永远不会**修改规则 —— 不存在「为了通过测试而改规则」的路径；改规则必须人工编辑并保存为新版本。
3. 通过与否由可执行断言决定（状态码、JSON 字段、数据归属、重复请求结果……），不只看 HTTP 200。
4. 报告中的结论关联真实请求与响应证据；证据全部经过脱敏（Authorization/Cookie/密码/token）。
5. 超时、连接失败归类为「无法判断」，绝不当作业务失败。
6. 符合已确认新需求的行为变化，标记为「符合已确认需求的行为变化」，不算回归。
7. 报告明确声明覆盖范围有限，不声称「整个项目正确」或「没有安全漏洞」。
8. LLM 仅用于**建议**规则；未配置模型 Key 时，手动编写规则、运行、导出报告全部可用，且不会伪造模型输出。

## 结果分类

| 分类 | 含义 |
| --- | --- |
| 通过 | 修改前后均通过 |
| 本次引入的回归 | 修改前通过、修改后失败 |
| 原有问题 | 修改前后都失败 |
| 新需求满足 | 新需求检查在修改后通过（修改前可以不满足） |
| 新需求未满足 | 新需求检查在修改后仍失败 |
| 符合已确认需求的行为变化 | 旧行为改变，但被一条已确认的新需求检查覆盖（`expected_change_ref`） |
| 相比修改前已改善 | 修改前失败、修改后通过 |
| 无法判断 | 服务不可达 / 超时 / 检查未完成（不算通过也不算失败） |

## 目录结构

```
demo/                 两个本地示例服务（缺陷只存在于这里）
  services.py         baseline（正确）与 candidate（含 5 个标注缺陷）的同一份实现
  run_baseline.py     启动修改前环境  http://127.0.0.1:8620
  run_candidate.py    启动修改后环境  http://127.0.0.1:8621
verifier/             验收器本体
  models.py           规则结构 + 校验
  executor.py         HTTP 执行器（断言、变量提取、超时/取消、脱敏证据）
  classification.py   结果分类与总体结论
  runner.py           两环境编排、进度持久化、取消
  db.py               SQLite（任务 / 规则版本 / 运行记录）
  report.py           Markdown / JSON 报告
  llm.py              可选模型建议（无 Key 时明确报错）
  openapi_import.py   OpenAPI 导入
  main.py             FastAPI 应用 + 静态页面
  static/             网页界面（原生 JS，无构建步骤）
tests/                pytest（分类、执行器、脱敏、LLM 降级、安全限制、端到端演示、API 流程）
scripts/smoke_check.py 对真实运行中的服务做一次冒烟验收
run_demo.py          一键启动三个服务
```

## 快速启动

Windows（PowerShell），需要 Python 3.10+：

```powershell
cd H:\xin\code\change-verifier
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 一键启动三个服务（baseline 8620 / candidate 8621 / 验收器 8622）
.\.venv\Scripts\python.exe run_demo.py
```

也可以分三个终端启动：

```powershell
.\.venv\Scripts\python.exe demo\run_baseline.py     # 修改前环境
.\.venv\Scripts\python.exe demo\run_candidate.py    # 修改后环境（含演示缺陷）
.\.venv\Scripts\python.exe -m verifier              # 验收器网页
```

## 网页演示操作步骤（完整验收流程）

1. 打开 <http://127.0.0.1:8622>。
2. 点击左侧 **「创建演示任务」**：自动生成任务（含需求）、接口清单、已确认的演示规则 v1（10 条检查）。
   *示例服务的 5 个缺陷可通过 <http://127.0.0.1:8621/api/meta/defects> 查看。*
3. 进入 **「验收规则」** 页：查看 / 编辑规则。可以：
   - 「加载演示规则」「＋ 添加检查」手动编辑；
   - 点击「保存并确认为新版本」——规则只增不改，旧版本永久保留；
   - 点击「AI 建议（可选）」：未配置 `LLM_API_KEY` 时会明确提示「请手动编写规则」，不会伪造结果。
4. 切到 **「运行与报告」**，点击 **运行验收**：相同的 10 条检查分别在 8620 和 8621 各跑一遍。
5. 查看结果。预期：
   - **发现 4 项本次引入的回归**：跨用户读订单、未登录访问订单列表、普通用户调用管理接口、重复创建不幂等；
   - 1 项新需求满足（`{items,total}` 结构）、1 项新需求未满足（`page_size` 被忽略）；
   - 1 项符合已确认需求的行为变化（列表结构变更）、1 项原有问题（不存在的订单返回 500）、2 项通过。
   - 展开任意一条：左「修改前」/ 右「修改后」并列展示请求、响应与逐条断言（密码/token 已脱敏）。
6. 点击 **Markdown / JSON** 导出报告。
7. （可选）点击左侧 **「演示：不可达环境」** 再运行一次：修改后环境指向未启动的端口，
   全部检查会归类为 **「无法判断」** —— 超时/连接失败不会被误判成业务错误。
8. （可选）把 candidate 服务的缺陷删掉再跑（见 `demo/services.py` 中 `DEMO DEFECT` 注释），
   验证回归项全部转回「通过」。

不做网页操作也可以直接冒烟验证（三个服务已启动时）：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_check.py
```

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

测试会自己在随机端口启动真实的 baseline/candidate 服务，通过验收器 API 完成端到端验收，
并断言：演示矩阵结果分类完全一致、报告不泄露密码与 token、不可达环境全部归类为「无法判断」等。

### 本仓库实际运行过的测试结果

2026-10-15 在本机（Windows · Python 3.13.14 · pytest 9.1.1）实际执行 `python -m pytest tests -q`：

```
49 passed in 70.01s
```

| 文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| tests/test_e2e_demo.py | 2 | 真实示例服务 + 验收器 API 端到端矩阵（4 回归 / 1 未满足 / 1 原有问题 / 1 行为变化 / 1 满足 / 2 通过）；报告含证据且凭据脱敏；不可达环境全部「无法判断」 |
| tests/test_executor.py | 8 | 登录提取 token 并复用；断言失败的证据保留；500 是真实响应不算「无法判断」；超时 / 连接失败 / 取消 → 无法判断；模板变量与类型保持 |
| tests/test_classification.py | 10 | 8 种分类、结论优先级、行为变化引用（含引用不通过时仍判回归） |
| tests/test_safety.py | 3 | 默认只允许本地目标；远程写入需二次确认；非法 URL 拒绝 |
| tests/test_api_flow.py | 6 | 规则版本不可变与确认；未出结果导出报告 409；未配置 Key 时 AI 建议明确报错 |
| tests/test_llm.py | 6 | 无 Key 明确报错（不伪造）；解析模型输出；坏输出报错 |
| tests/test_redaction_and_validation.py | 8 | 脱敏（头 / 键名 / 变量值）与规则校验 |
| tests/test_runner_partial.py | 2 | 未执行检查 → 无法判断；引用未执行的新需求检查不能洗白回归 |
| tests/test_jsonpath.py | 4 | JSON 路径解析与缺失哨兵 |

另外实际运行过的冒烟验证（真实端口 8620/8621/8622，非测试替身）：

```
> python scripts/smoke_check.py
[ok] 运行状态：completed
[ok] 结论：发现 4 项本次引入的回归，建议修复后再上线
     符合已确认需求的行为变化: 1
     新需求未满足: 1
     通过: 2
     原有问题: 1
     本次引入的回归: 4
     新需求满足: 1
[ok] Markdown 报告不含演示密码（已脱敏）
```

网页流程也已用自动化浏览器实际操作验证过一次：创建演示任务 → 运行验收 → 展开「跨用户读取」回归，
页面并列展示修改前 `403` 与修改后 `200（bob 的订单 102）` 的真实请求响应，0 个控制台错误。
样例报告见 `docs/sample_report.md`（由上面这次运行导出）。

## 规则格式（一条检查的 JSON）

```json
{
  "id": "read-other-order-denied",
  "name": "用户 A 不能读取用户 B 的订单",
  "category": "existing_behavior",          // 或 new_requirement
  "expectation": "alice 读取 bob 的订单 102 必须返回 403",
  "expected_change_ref": null,              // 旧行为被新需求替代时，填新需求检查的 id
  "steps": [
    {
      "name": "登录",
      "method": "POST", "path": "/api/login",
      "json": {"username": "alice", "password": "alice-password"},
      "extract": {"token": {"source": "json", "path": "$.access_token", "sensitive": true}},
      "assert": [{"type": "status", "op": "equals", "value": 200}]
    },
    {
      "name": "读取 bob 的订单",
      "method": "GET", "path": "/api/orders/102",
      "headers": {"Authorization": "Bearer {{token}}"},
      "assert": [{"type": "status", "op": "equals", "value": 403}]
    }
  ]
}
```

- 变量：`{{nonce}}`（每次运行唯一，用于测试数据隔离）、`{{check_id}}`、`{{env}}`，以及前面步骤 `extract` 出来的变量；
  整个字符串就是一个占位符时保持原始类型（数字不会变字符串）。
- 断言 `type`：`status`（equals / in）、`json`（equals / not_equals / contains / not_contains / exists / not_exists /
  type / regex / length_equals / greater_than / less_than / in）、`header`、`text`。
- `extract.source`：`json`（JSON 路径）、`header`、`status`、`text`（正则）。

## 安全与隔离设计

- **默认只允许操作本地环境**：目标不是 `127.0.0.1/localhost` 时拒绝运行，除非在任务里显式开启
  「允许远程环境」；即使开启，远程**写入**（POST/PUT/PATCH/DELETE）还需要再开「允许远程写入」。
- **检查之间隔离**：写操作使用 `{{nonce}}`（每次运行唯一）标记的测试数据，检查之间彼此独立；
  只读检查引用两个服务一致的种子数据（相同订单号）。示例服务提供 `POST /api/debug/reset` 恢复种子数据。
- **凭据不落库、不进报告**：证据在写入 SQLite 前就完成脱敏；`Authorization/Cookie/Set-Cookie` 等头、
  键名含 `password/token/secret/api_key` 的字段、以及运行中提取出的敏感变量值，全部替换为 `[REDACTED]`。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VERIFIER_DB` | `verifier_data/verifier.db` | SQLite 路径 |
| `VERIFIER_HOST` / `VERIFIER_PORT` | `127.0.0.1` / `8622` | 验收器监听地址 |
| `DEMO_BASELINE_PORT` / `DEMO_CANDIDATE_PORT` | `8620` / `8621` | 两个示例服务端口 |
| `LLM_API_KEY`（或 `OPENAI_API_KEY`/`OPENCODE_API_KEY`） | 无 | 配置后才能用「AI 建议」 |
| `LLM_BASE_URL` / `LLM_MODEL` | `https://api.openai.com/v1` / `gpt-4o-mini` | OpenAI 兼容接口 |

## 已知限制（诚实声明）

- 只覆盖网页上列出的检查项，不代表整个项目正确，也不代表没有安全漏洞。
- 只支持 HTTP API 验收；不做浏览器操作、不做并发/性能/稳定性验证。
- 需要用户能提供两个可访问的环境；示例服务把状态放在内存里，重启即恢复种子数据。
- 「无法判断」只区分到「环境/执行问题」这一层；若要区分「测试环境故障 vs 被测代码导致超时」需要更多信息（如服务端日志接入）。
- LLM 建议的质量取决于模型与需求描述的清晰度；建议必须人工确认，MVP 不做自动确认。
- 取消运行是「当前请求完成后停止」，不是强杀（httpx 请求不中断）。
- 单机单进程执行（后台线程）；没有多用户、并发队列、权限控制，仅适合本地/开发机使用。
- SQLite 存储与运行历史没有清理策略。

## 下一步最值得验证的用户需求

1. **真实项目的规则编写成本**：独立开发者愿不愿意为每次改动写 3~10 条规则？先观察「AI 建议 + 人工确认」是否显著降低门槛。
2. **回归判定是否被信任**：「本次引入的回归」的误报率是第一生命线；建议先用真实 git 改动做 5~10 次盲测。
3. **环境准备**：真实项目通常需要跑迁移/造数据/起服务，验证是否需要在任务上加「环境准备步骤（setup）Hook」。
4. **报告落地场景**：报告是否会真的被贴进 PR / 给 AI 助手回喂修复指令？这决定报告的字段与格式优先级。
5. **规则自然语言化**：用户能否接受「先写自然语言验收点，再翻译成步骤」，还是直接编辑 JSON 更顺手。
