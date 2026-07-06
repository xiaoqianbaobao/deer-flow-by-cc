# DeerFlow Harness 嵌入业务系统——技术方案

> 将 DeerFlow Harness 作为 Agent 执行内核嵌入现有业务系统，企业快速获得 AI Agent 能力。

---

## 1. 背景与目标

### 1.1 背景

业务系统（ERP/OA/CRM/SaaS 平台等）需要 AI 能力——自然语言查数据、自动生成报表、智能审批、代码生成——但不想从零搭建 Agent 引擎。

DeerFlow Harness 是一个经过生产验证的 Agent 运行时引擎，提供了：

- 完整的 Agent 执行管线（14 层中间件链）
- 多模型支持（DeepSeek/OpenAI/Claude/本地模型）
- 沙箱执行环境 + bash 安全审计
- 子任务委派（subagent）与技能插件系统
- 状态持久化（checkpointer + store）

### 1.2 目标

- 将 Harness 嵌入业务系统，不改变原有架构
- 业务系统通过 API 调用 Agent 能力
- 复用公司已有基础设施（MySQL/Redis/认证体系）
- 支持水平扩展和高可用部署

---

## 2. 整体架构

```
                     ┌─────────────────────────────────┐
                     │        你的业务系统               │
                     │  (Java/Go/Python/Node...)        │
                     │  业务 API  ← 用户请求             │
                     └──────────┬──────────────────────┘
                                │ HTTP/gRPC
                                ▼
               ┌─────────────────────────────────┐
               │        Agent API Service          │
               │  (Python FastAPI ± 50 行)         │
               │  POST /api/agent/chat             │
               │  POST /api/agent/stream           │
               │  POST /api/agent/task             │
               └──────────┬──────────────────────┘
                          │ 函数调用
                          ▼
┌────────────────────────────────────────────────────────────┐
│                  DeerFlow Harness 内核                       │
│                                                            │
│  create_deerflow_agent() → 14层中间件链 → agent.astream()   │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Config                │  Model Factory              │   │
│  │  (config.yaml)         │  (DeepSeek/OpenAI/Claude)   │   │
│  ├────────────────────────┼─────────────────────────────┤   │
│  │  Checkpointer + Store  │  StreamBridge               │   │
│  │  (MySQL/Postgres)      │  (内存/Redis)                │   │
│  ├────────────────────────┼─────────────────────────────┤   │
│  │  Sandbox               │  Subagent Pool              │   │
│  │  (Local/Docker/K8s)    │  (3×3 线程池)               │   │
│  ├────────────────────────┼─────────────────────────────┤   │
│  │  Tools + MCP           │  Skills                     │   │
│  │  (bash/file/search)    │  (SKILL.md 插件)            │   │
│  └────────────────────────┴─────────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

**关键原则**：Agent API Service 是你和 Harness 之间的薄胶水层。它不处理业务逻辑，只负责将业务系统的请求转换为 Harness 调用，并将结果返回。

---

## 3. 依赖集成（pip install）

Harness 本身是一个可发布的 Python 包：

```bash
# 方式一：直接复制源码
cp -r deer-flow-by-cc/backend/packages/harness/deerflow your-project/vendor/

# 方式二：发布为 pip 包（推荐）
cd deer-flow-by-cc/backend/packages/harness
# 发布到私有 PyPI / Git仓库
pip install deerflow-harness
```

**依赖清单**（最小子集）：

```
# 核心
langchain>=0.3,<1.0      # LangChain 框架
langgraph>=0.5,<1.0      # LangGraph Agent 引擎

# 模型
langchain-openai          # OpenAI / 兼容接口
langchain-anthropic       # Claude（可选）

# 持久化
langgraph-checkpoint-sqlite  # SQLite checkpoint（开发）
langgraph-checkpoint-postgres # Postgres checkpoint（生产）

# 沙箱
docker / podman           # Docker sandbox（可选）

# 搜索（可选）
tavily-python             # Web 搜索
jina                       # Jina AI

# MCP（可选）
langchain-mcp-adapters    # MCP 客户端
```

---

## 4. Agent API 设计

```python
# agent_service.py — 业务系统调用 Harness 的唯一入口

from dataclasses import dataclass
from typing import AsyncIterator
from deerflow.agents import create_deerflow_agent
from deerflow.agents.features import RuntimeFeatures
from deerflow.agents.checkpointer import get_checkpointer
from deerflow.models import create_chat_model
from langchain_core.messages import HumanMessage


@dataclass
class AgentRequest:
    user_id: str                # 业务系统的用户 ID
    tenant_id: str | None       # 租户 ID（多租户场景）
    conversation_id: str        # 对话 ID（用于 checkpoint 恢复）
    message: str                # 用户消息
    tools: list[str] | None     # 允许使用的工具列表（None=全部）
    model: str | None           # 模型名称（None=默认）
    system_prompt: str | None   # 补充 system prompt（None=默认）


@dataclass
class AgentEvent:
    event: str                  # "token" / "tool_call" / "done" / "error"
    data: str | dict
    conversation_id: str


class AgentService:
    """Harness 的薄封装层"""

    def __init__(self, config_path: str):
        # 全局初始化一次
        self._agent_fn = lambda: create_deerflow_agent(
            model=create_chat_model(),
            checkpointer=get_checkpointer(),
            features=RuntimeFeatures(
                sandbox=True,
                subagent=True,
                memory=False,         # 按需开启
                vision=False,
            ),
        )

    async def chat(self, req: AgentRequest) -> str:
        """同步对话（等全部结果返回）"""
        state = await self._build_state(req)
        agent = self._agent_fn()
        result = await agent.ainvoke(state)
        return result["messages"][-1].content

    async def stream(self, req: AgentRequest) -> AsyncIterator[AgentEvent]:
        """流式对话（逐个 token 返回）"""
        state = await self._build_state(req)
        agent = self._agent_fn()
        async for chunk in agent.astream(state, stream_mode="messages"):
            # chunk = (message_chunk, metadata)
            msg, meta = chunk
            if msg.type == "ai" and msg.content:
                yield AgentEvent("token", msg.content, req.conversation_id)
            if msg.type == "ai" and msg.tool_calls:
                yield AgentEvent("tool_call", msg.tool_calls, req.conversation_id)
        yield AgentEvent("done", "", req.conversation_id)

    async def _build_state(self, req: AgentRequest) -> dict:
        """构建 ThreadState"""
        messages = []
        if req.system_prompt:
            messages.append(SystemMessage(req.system_prompt))
        messages.append(HumanMessage(req.message))

        state = {"messages": messages}

        # 身份注入（Harness 的 IdentityMiddleware 会读取）
        if req.user_id:
            state["identity"] = {
                "user_id": int(req.user_id) if req.user_id.isdigit() else req.user_id,
                "tenant_id": int(req.tenant_id) if req.tenant_id else None,
                "permissions": req.tools or [],
            }

        return state
```

**FastAPI 胶水层**（约 50 行）：

```python
# api.py — 业务系统 HTTP 入口

from fastapi import FastAPI, HTTPException
from agent_service import AgentService, AgentRequest

app = FastAPI()
agent = AgentService("config.yaml")

@app.post("/api/agent/chat")
async def chat(user_id: str, conversation_id: str, message: str):
    req = AgentRequest(
        user_id=user_id,
        conversation_id=conversation_id,
        message=message,
    )
    try:
        result = await agent.chat(req)
        return {"conversation_id": conversation_id, "reply": result}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/agent/stream")
async def stream(user_id: str, conversation_id: str, message: str):
    req = AgentRequest(
        user_id=user_id,
        conversation_id=conversation_id,
        message=message,
    )
    return StreamingResponse(agent.stream(req), media_type="text/event-stream")
```

---

## 5. 配置管理

```yaml
# config.yaml — 独立于业务系统的配置文件

models:
  - name: deepseek-v3
    display_name: DeepSeek V3
    use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
    model: deepseek-chat
    api_key: $DEEPSEEK_API_KEY      # 从环境变量读取
    timeout: 600.0
    supports_thinking: true

checkpointer:
  type: sqlite                       # 开发期
  connection_string: ".deer-flow/checkpoints.db"

# 生产环境切换到 MySQL（自定义 MySQLSaver）
# checkpointer:
#   type: mysql
#   connection_string: "mysql+aiomysql://user:pass@host:3306/deerflow_checkpoints"

sandbox:
  type: local                        # 开发期
  # type: docker                     # 生产期（隔离更严格）
```

**配置热重载**：Harness 的 `AppConfig` 支持 mtime 检测自动重载。改动配置后无需重启服务。

---

## 6. 身份集成（Identity Bridge）

这是嵌入企业系统最关键的一层。你的业务系统已有的用户/角色/权限体系，需要映射到 Harness 的权限模型。

```python
# identity_bridge.py — 将业务系统的身份映射到 Harness

class IdentityBridge:
    """将业务系统的用户/权限翻译为 Harness 的 Identity"""

    def __init__(self, user_service, permission_service):
        self._users = user_service          # 你的用户服务
        self._perms = permission_service    # 你的权限服务

    async def build_harness_identity(self, biz_user_id: str) -> dict:
        """构建 Harness state["identity"]"""
        user = await self._users.get(biz_user_id)
        perms = await self._perms.get(biz_user_id)

        # 工具权限映射（业务角色 → Harness 工具）
        tool_permissions = set()
        if "developer" in user.roles:
            tool_permissions.update(["bash", "read_file", "write_file"])
        if "analyst" in user.roles:
            tool_permissions.update(["read_file", "search", "code_runner"])

        return {
            "user_id": user.id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "permissions": frozenset(tool_permissions),
        }
```

**`IdentityGuardrailMiddleware` 会自动拦截无权工具调用**。例如一个 "analyst" 角色调用 bash 会被拒绝：

```json
{
  "status": "error",
  "content": "Permission denied: missing permission 'thread:write' for tool 'bash'"
}
```

---

## 7. 自定义工具（你的业务能力）

这是嵌入的价值所在——把你业务系统的能力暴露为 Agent 可以调用的工具。

```python
# biz_tools.py — 你的业务工具

from langchain.tools import tool

@tool("query_order")
def query_order(order_id: str) -> str:
    """查询订单状态。order_id: 订单号"""
    # 调用你的业务系统 API
    return f"订单 {order_id} 状态: 已发货, 预计送达: 2026-07-08"

@tool("create_report")
def create_report(template: str, params: str) -> str:
    """生成报表。template: 模板名, params: JSON参数字符串"""
    # 调用你的报表系统
    return f"报表已生成，下载地址: /reports/{template}_{datetime.now():%Y%m%d}.pdf"

@tool("search_knowledge_base")
def search_knowledge_base(query: str) -> list[dict]:
    """搜索内部知识库。query: 搜索关键词"""
    # 调用你的知识库
    return [
        {"title": "员工报销流程", "url": "/wiki/expense"},
        {"title": "IT 支持手册", "url": "/wiki/it-support"},
    ]
```

**注册到 Agent**：

```python
from biz_tools import query_order, create_report

agent = create_deerflow_agent(
    model=create_chat_model(),
    features=RuntimeFeatures(sandbox=False),    # 业务场景可能不需要沙箱
    tools=[query_order, create_report, search_knowledge_base],
)
```

---

## 8. 完整请求流

```
用户: "帮我查一下订单 ORD-2026-001 的状态"
  │
  ▼
你的前端 → 你的后端 API → AgentService.chat()
  │
  ├─ IdentityBridge.build_harness_identity("user_123")
  │     ↳ 查询角色 → "analyst"
  │     ↳ 映射权限 → frozenset({"read_file", "search", "query_order"})
  │
  ├─ create_deerflow_agent() → compiled graph
  │
  ├─ agent.astream(state, stream_mode="values")
  │     │
  │     ├─ 14层中间件链处理
  │     ├─ LLM 返回: 需要查订单 → tool_call("query_order")
  │     │
  │     ├─ IdentityGuardrailMiddleware 检查权限
  │     │     ↳ query_order 不在 tool_permission_map 中
  │     │     ↳ 检查 tool.required_permission → 无 → 默认 skill:invoke
  │     │     ↳ 但有 "analyst" 角色 → 放行
  │     │
  │     ├─ 执行 query_order("ORD-2026-001")
  │     │     ↳ 调用业务系统 API → 返回 "已发货"
  │     │
  │     ├─ LLM 再次调用 → 生成回答
  │     ├─ checkpoint.aput() 持久化
  │     └─ checkpoint 写入 MySQL
  │
  └─ 返回: "ORD-2026-001 已发货，预计明天到"
```

---

## 9. 部署方案

### 最小部署（1 台机器）

```
┌─────────────────────────────┐
│  应用服务器（8C 16G）         │
│                             │
│  Nginx → 业务系统 API         │
│         → Agent Service     │  ← Python FastAPI 进程
│             ↳ 嵌入 Harness   │
│                             │
│  MySQL（已有实例）             │
│  ├─ checkpoint_writes        │
│  ├─ checkpoint_blobs         │
│  └─ 你的业务表                │
│                             │
│  Redis（已有实例）             │
│  └─ StreamBridge（未来）      │
└─────────────────────────────┘
```

### 生产部署（多实例）

```
┌──────────┐
│   Nginx  │  负载均衡
└────┬─────┘
     │
     ├──────────────┬──────────────┐
     ▼              ▼              ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│ Agent    │ │ Agent    │ │ Agent    │  ← 无状态，水平扩展
│ Service 1│ │ Service 2│ │ Service 3│     checkpointer 共享 MySQL
└──────────┘ └──────────┘ └──────────┘
     │              │              │
     └──────────────┼──────────────┘
                    ▼
           ┌────────────────┐
           │  MySQL（共享）    │
           │  checkpoint表   │
           │  + 你的业务表    │
           └────────────────┘
                    │
                    ▼
           ┌────────────────┐
           │  Redis（共享）    │
           │  StreamBridge   │  ← 多实例 SSE 推送
           └────────────────┘
```

---

## 10. 方案对比

| | DeerFlow Harness | 自研 Agent 引擎 | Dify / FastGPT |
|---|---|---|---|
| **集成难度** | 低（pip install + ~50 行胶水） | 高（从零造轮子） | 低（但需改造源系统） |
| **模型支持** | 10+ provider，配置驱动 | 自己写适配器 | 固定几家 |
| **沙箱与安全** | 成熟（SandboxAuditMiddleware） | 自己写 | 有限 |
| **状态持久化** | checkpointer 3 后端 | 自己实现 | 托管 |
| **多租户** | IdentityGuardrail + HMAC 传播 | 自己实现 | 有限 |
| **定制自由度** | 完全控制（开放全部源码） | 完全控制 | 受平台限制 |
| **适用场景** | 需要深度集成+完全掌控的企业 | 有充足 AI 团队 | 快速验证不想碰代码 |

---

## 11. 建议的实施步骤

```
第 1 周：
  □ pip install deerflow-harness
  □ 写 Agent API Service（~50 行 FastAPI）
  □ 配置 DeepSeek API Key
  □ 跑通第一个对话

第 2 周：
  □ 写 IdentityBridge（接入公司认证）
  □ 写 1-2 个业务工具（query_order / search_employee）
  □ 配置 checkpointer 指向公司 MySQL
  □ 沙箱安全策略确认

第 3 周：
  □ 前端集成（WebSocket / SSE 对接）
  □ 权限映射全量覆盖
  □ 压力测试 / 配置调优

第 4 周：
  □ 生产部署（Nginx + 多实例）
  □ 监控与告警接入
  □ 使用文档 / 培训
```

---

## 12. 风险与应对

| 风险 | 应对 |
|------|------|
| Harness 依赖 LangGraph 版本升级 | 在 CI 中锁定 langgraph 版本号，用 pip freeze 固定依赖 |
| Python 技术栈 vs 公司 Java/Go 栈 | Agent API Service 作为一个独立微服务部署，业务系统只通过 HTTP 调用 |
| Sandbox 安全合规 | 生产环境必须用 Docker sandbox（不要用 local mode），SandboxAuditMiddleware 保持开启 |
| Checkpoint 磁盘膨胀 | P0 方案（清 viewed_images）+ 写入前 zlib 压缩 blob + 定期裁剪 |
| LLM 响应延迟 | Agent API Service 接业务系统的超时配置，展示层做流式效果 |

---

*文档版本 v1 | 基于 DeerFlow Harness 源码分析 | 2026-07-06*
