# MCP + Playwright GUI Agent

基于 **LangGraph（Planner–Actor–Critic）+ Playwright MCP Server** 的浏览器自动化智能体。用户用一句自然语言下达任务，Agent 自动拆解步骤、驱动真实浏览器点击/输入/滚动，并把每一步的思考链（CoT）与带红框编号的实时截图通过 SSE 推送到前端监控台。

## 核心特性

- **DOM 蒸馏 + Set-of-Mark**：注入 `dom_distiller.js`，只保留可视区内可交互元素，压缩成 `[1] <button>登录</button>` 这样的极简列表，并给真实 DOM 打上 `data-mcp-id` 与可视化红框编号，用数字编号代替脆弱的选择器。
- **Plan–Act–Critic 闭环**：Planner 拆解子目标，Actor 选择 MCP 工具执行，Critic 校验结果；遇到弹窗遮挡、选择器失效等错误自动回退 Planner 重规划（动态自愈），连续失败超过阈值则熔断结束。
- **人机验证转人工**：自动识别 CAPTCHA / Cloudflare 挑战 / 滑块，暂停自动操作并在前端提示，人工完成验证后自动恢复执行。
- **实时可视化**：FastAPI 以 SSE 流式推送每个节点的思考与浏览器截图，前端 Vue 单页实时展示计划、日志和画面。
- **可断点、可回放**：`AsyncSqliteSaver` 将每一步状态写入 `agent_checkpoints.sqlite`，可按 `session_id` 读取历史轨迹。
- **安全边界**：`browser_navigate` 仅允许 `http/https` 完整网址，拒绝 `file://`、`javascript:` 等协议。

## 架构

```text
用户目标 (goal)
   │
   ▼
┌──────────┐   plan_steps   ┌──────────┐  tool_call  ┌─────────────────────────┐
│ Planner  │ ─────────────▶ │  Actor   │ ──────────▶ │ Playwright MCP Tools    │
└──────────┘                └──────────┘             │ navigate/click/fill/    │
      ▲                          │                   │ scroll/screenshot       │
      │ replan                   ▼                   └─────────────────────────┘
      │                     ┌──────────┐
      └──────────────────── │  Critic  │ ── 通过 ──▶ 下一步 / 最终总结
        (❌ 报错或未调用工具)  └──────────┘
```

- 正常步进：`critic → actor`（执行下一个子目标）
- 异常自愈：`critic → planner`（结合最新 DOM 重新规划剩余步骤）
- 结束：任务完成或 `error_count >= MAX_ERROR_COUNT`

## 目录结构

```text
06_mcp_playwright_agent/
├── backend/
│   ├── main.py                      # FastAPI：SSE 流式接口 + 历史轨迹接口
│   ├── requirements.txt
│   ├── agent/
│   │   ├── state.py                 # AgentState（TypedDict）状态定义
│   │   └── graph.py                 # Planner / Actor / Critic 节点与条件边
│   └── mcp_server/
│       ├── browser_mcp_server.py    # FastMCP Server，暴露 5 个浏览器工具
│       └── dom_distiller.js         # 页面 DOM 蒸馏 + Set-of-Mark 注入脚本
├── frontend/
│   └── index.html                   # Vue 3 单页监控台（思考链 + 实时截图）
├── .env.example                     # 环境变量模板
└── README.md
```

## 快速开始

### 1. 环境要求

- Python 3.10+（项目在 3.11 上验证）
- 可访问 OpenAI 兼容接口的大模型（示例使用 DeepSeek）
- Chromium（由 Playwright 自动下载）

### 2. 安装依赖

```bash
pip install -r backend/requirements.txt
playwright install chromium
```

> 注意：`mcp` 依赖被锁定为 `>=1.2.0,<2`，2.x 版本与当前代码不兼容。

### 3. 配置环境变量

复制模板并在项目**根目录**创建 `.env`（`.env` 已被 `.gitignore` 忽略）：

```bash
cp .env.example .env
```

```ini
# ===== 大模型（OpenAI 兼容接口）=====
DEEPSEEK_API_KEY=sk-xxxxxxxx
BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
LLM_TEMPERATURE=0

# ===== FastAPI 后端 =====
APP_HOST=127.0.0.1
APP_PORT=8000

# ===== LangGraph 持久化 =====
DB_PATH=agent_checkpoints.sqlite
MAX_ERROR_COUNT=3

# ===== Playwright 浏览器 =====
# 人机验证需要人工在可见浏览器中操作，必须为 false
BROWSER_HEADLESS=false
BROWSER_VIEWPORT_WIDTH=1280
BROWSER_VIEWPORT_HEIGHT=800
NAV_TIMEOUT_MS=15000
CLICK_TIMEOUT_MS=5000
SCREENSHOT_QUALITY=60

# ===== 人机验证人工介入 =====
HUMAN_WAIT_TIMEOUT_MS=180000
HUMAN_POLL_INTERVAL_MS=3000
```

当前代码实际读取的是 `DEEPSEEK_API_KEY`、`BASE_URL`、`MODEL_NAME`、`LLM_TEMPERATURE`、`DB_PATH`、`MAX_ERROR_COUNT`，以及 `BROWSER_*`、`HUMAN_*` 全部浏览器与人机验证参数；`APP_*` 仍需通过 uvicorn 启动参数指定。

### 4. 启动后端

在项目根目录执行。**Windows 下必须显式使用 ProactorEventLoop**，否则 Playwright 无法拉起浏览器子进程：

```bash
uvicorn backend.main:app --reload --loop backend.main:proactor_loop_factory
```

若不需要热重载，也可以直接运行：

```bash
uvicorn backend.main:app --loop backend.main:proactor_loop_factory
```

### 5. 打开前端

直接用浏览器打开 [frontend/index.html](file:///g:/agent_stu/projects/06_mcp_playwright_agent/frontend/index.html)（前端默认请求 `http://localhost:8000`）。

输入指令，例如：

```text
打开 https://www.bing.com 搜索 LangGraph 并总结前两条结果
```

点击「启动 Agent」后，左侧实时显示 Planner/Actor/Critic 思考链与当前计划，右侧显示带红框编号的浏览器截图。

## MCP 工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `browser_navigate` | `url: str` | 打开网页（仅 http/https），返回蒸馏后的可交互 DOM 列表 |
| `browser_click` | `som_id: int` | 按 Set-of-Mark 编号点击元素，失败时返回最新 DOM 供自愈 |
| `browser_fill` | `som_id: int, text: str, press_enter: bool = True` | 在输入框填入文本，可选回车提交 |
| `browser_scroll` | `direction: str` | 向上/向下滚动一屏（`up` / `down`） |
| `browser_wait_for_human` | `timeout_seconds: int = 0` | 遇到人机验证时暂停并轮询等待人工完成，0 表示使用 `HUMAN_WAIT_TIMEOUT_MS` |
| `browser_screenshot` | — | 返回带红框编号的 JPEG Base64 截图 |

## 人机验证（CAPTCHA）处理

当目标网站出现 reCAPTCHA / hCaptcha / Cloudflare Turnstile / 滑块等验证时，Agent 不再盲目乱点直至熔断，而是自动转人工：

1. `dom_distiller.js` 在 DOM 蒸馏时同步识别验证组件（验证 iframe、`.g-recaptcha`、`.cf-turnstile`、`#challenge-form`、`Just a moment...` 标题与强提示文案），并写入 `verification.detected / reasons`；
2. 各浏览器工具在命中验证时，会在结果末尾追加 `🛑 需要人工验证` 标记，Actor 检测到后由 Critic 路由到 `human_intervention` 节点；
3. `human_intervention` 节点暂停自动操作，通过 SSE 的 custom 流立即通知前端（页面顶部出现「等待人工验证」横幅），每 10 秒推送一次等待进度；
4. `browser_wait_for_human` 轮询页面直到验证消失：人工在浏览器窗口中完成验证后，Agent 自动重置错误计数、回到 Planner 基于最新 DOM 重新规划并继续执行；超过 `HUMAN_WAIT_TIMEOUT_MS` 未通过则计入一次失败。

> 该能力依赖可见浏览器（`BROWSER_HEADLESS=false`）。无头模式下人工无法操作界面，节点会直接给出配置提示而不空等。

`browser_mcp_server.py` 也可作为独立 stdio MCP Server 运行：

```bash
python -m backend.mcp_server.browser_mcp_server
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/agent/stream?goal=...&session_id=...` | SSE 流，逐节点推送 `node` / `thought` / `plan_steps` / `screenshot` / `final_answer` |
| `GET` | `/api/agent/history/{session_id}` | 返回该会话的全部检查点快照（轨迹回放） |

## 已知限制

- 浏览器实例为**全局单例**，多会话并发会互相覆盖页面状态；生产环境建议按 `session_id` 隔离上下文并加并发锁。
- 依赖 `AsyncSqliteSaver`；删除 `agent_checkpoints.sqlite` 即清空历史轨迹。
- DOM 蒸馏只覆盖当前视口附近的元素，目标元素不在视口内时需先滚动。
- 仅支持 `http/https`，刻意不支持本地文件与 `javascript:` 地址。
- 人机验证只做“识别 + 暂停等待人工”，不做任何自动绕过；无头模式无法人工介入，必须使用 `BROWSER_HEADLESS=false`。
