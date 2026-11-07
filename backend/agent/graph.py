import json
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage,HumanMessage
from langgraph.graph import StateGraph,END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_stream_writer

from backend.agent.state import AgentState
from backend.mcp_server.browser_mcp_server import (
    HUMAN_VERIFY_MARKER,
    HEADLESS,
    browser_navigate,
    browser_click,
    browser_fill,
    browser_scroll,
    browser_screenshot,
    get_page,
    verification_brief,
    wait_for_human_verification,
)

#读取项目根目录下的 .env
load_dotenv()

#连续失败阈值（可从 .env 调整，避免死循环消耗 Token）
MAX_ERROR_COUNT = int(os.getenv("MAX_ERROR_COUNT", "3"))

#初始化支持Function Calling 的大模型
llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY") or None,
    base_url=os.getenv("BASE_URL") or None,
    model=os.getenv("MODEL_NAME", "deepseek-flash"),
    temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
)
#将MCP工具绑定给Actor 大模型
MCP_TOOLS = {
    "browser_navigate": browser_navigate,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_scroll": browser_scroll,
}
llm_with_tools = llm.bind_tools(list(MCP_TOOLS.values()))
# ==================== 节点 1：Planner（任务拆解与动态重规划） ====================
async def planner_node(state: AgentState) -> dict:
    """
    负责将用户的复杂意图拆解为多步子目标
    当Critic 发现弹窗遮挡或选择器报错时，结合当前简化DOM树，重新拆解任务子目标
    """
    is_replan = state.get("needs_replan", False)

    if not is_replan:
        prompt = (
            f"你是一个网页自动化规划专家(Planner)。\n"
            f"用户总目标：{state['user_goal']}\n"
            f"请将该任务拆解为 2~5 个清晰的网页操作子步骤，严格以 JSON 数组字符串格式返回，"
            f'例如：["1. 打开必应首页", "2. 在搜索框输入关键词并回车", "3. 提取首条结果标题"]'
        )
    else:
        #动态自愈重规划Prompt
        prompt = (
            f"⚠️ 网页执行遇到阻碍，触发动态自愈重规划！\n"
            f"用户总目标：{state['user_goal']}\n"
            f"原计划步骤：{state['plan_steps']}\n"
            f"上一步报错信息：{state['last_action_result']}\n"
            f"当前网页最新简化 DOM 树：\n{state['dom_snapshot']}\n\n"
            f"请分析失败原因（如存在弹窗关闭按钮需先点击、或者换用其他元素编号），"
            f"给出从现在开始剩余的修正步骤列表，严格以 JSON 数组格式返回。"
        )
    resp = await llm.ainvoke([HumanMessage(content=prompt)])
    try:
        steps = json.loads(resp.content.strip().removeprefix("```json").removeprefix("```"))
    except Exception:
        steps = [resp.content]

    thought = f"📋 [Planner] {'触发自愈重规划' if is_replan else '完成初始任务拆解'}：{steps}"

    return {
        "plan_steps": steps,
        "current_step_idx": 0,
        "needs_replan": False,
        "thought_trace": [thought],
    }

# ==================== 节点 2：Actor（页面感知与 MCP 动作执行） ====================
async def actor_node(state: AgentState) -> dict:
    """
    根据当前子目标和简化后的 Set-of-Mark DOM 树，决定调用哪个 MCP 浏览器工具。
    """
    current_step = state["plan_steps"][state["current_step_idx"]]

    sys_prompt = (
        "你是一个精准的网页执行智能体(Actor)。\n"
        "请仔细观察当前网页的 Set-of-Mark 简化 DOM 列表（形如 [编号] <标签>文字</标签>），"
        "选择一个最合适的工具完成【当前子目标】。\n"
        "注意：点击或输入时，必须传入方括号内的数字编号 som_id！\n"
        "必须调用工具，禁止只回复文字说明；即使当前子目标只是查看或汇报，也要先调用工具确认页面状态。"
    )
    user_prompt = (
        f"【总目标】：{state['user_goal']}\n"
        f"【当前子目标】：{current_step}\n"
        f"【当前页面简化 DOM】：\n{state.get('dom_snapshot', '尚未打开任何网页')}"
    )

    ai_msg = await llm_with_tools.ainvoke([
        SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)
        ])

    action_result = "未调用工具"
    thought = f"🤖 [Actor] 针对子目标「{current_step}」思考完成。"

    #解析并执行大模型发起的 MCP Tool Call
    if ai_msg.tool_calls:
        tool_call = ai_msg.tool_calls[0]
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        thought = f"🤖 [Actor] 决定调用 MCP 工具 `{tool_name}`，参数：{tool_args}"

        tool_fn = MCP_TOOLS.get(tool_name)
        if tool_fn is None:
            action_result = f"❌ 模型选择了不存在的工具 `{tool_name}`"
        else:
            action_result = await tool_fn(**tool_args)

    #动作执行后，顺手抓一张带红框标记的实时截图
    #浏览器可能被人工关闭，截图失败不应中断整个任务
    try:
        screenshot = await browser_screenshot()
    except Exception:
        screenshot = None

    #工具结果带有人机验证标记时，交给 Critic 路由到人工介入节点（不当作普通报错）
    needs_human_verify = HUMAN_VERIFY_MARKER in action_result

    return {
        "last_action_result": action_result,
        "dom_snapshot": action_result,
        "screenshot_b64": screenshot,
        "needs_human_verify": needs_human_verify,
        "thought_trace": [thought],
    }

# ==================== 节点 3：Critic（状态校验与自愈路由判断） ====================
async def critic_node(state: AgentState) -> dict:
    """
    校验 Actor 的执行结果：
    1. 如果工具返回 ❌ 报错或发现弹窗遮挡 -> 标记 needs_replan = True 触发自愈；
    2. 如果当前子目标完成且还有后续步骤 -> 推进 current_step_idx += 1；
    3. 如果所有子目标完成 -> 汇总最终答案并标记 is_completed = True。
    """

    last_res = state.get("last_action_result", "")
    idx = state["current_step_idx"]
    steps = state["plan_steps"]

    #情况0：页面命中人机验证，转交人工介入节点暂停等待（不计入错误次数）
    if state.get("needs_human_verify", False):
        thought = "🔍 [Critic] 页面出现人机验证拦截，转交人工验证节点暂停处理。"
        return {"thought_trace": [thought]}

    #情况A：检测到工具执行报错（例如弹窗遮挡、超时、选择器失效）
    #“未调用工具”说明模型跳过了页面操作，同样视为失败，避免子目标被空转判为通过
    if "❌" in last_res or last_res == "未调用工具":
        err_cnt = state.get("error_count", 0) + 1
        thought = f"🔍 [Critic] 检测到动作执行异常（第 {err_cnt} 次），准备回退至 Planner 重新规划路径！"
        return {
            "error_count": err_cnt,
            "needs_replan": True,
            "thought_trace": [thought],
        }

    #情况B：当前子步骤顺利完成，检查是否还有下一步
    if idx + 1 < len(steps):
        thought = f"🔍 [Critic] 子步骤「{steps[idx]}」校验通过 ✅，推进至下一子目标。"
        return {
            "current_step_idx": idx + 1,
            "needs_replan": False,
            "thought_trace": [thought],
        }
    #情况C：所有步骤执行完毕，生成最终总结
    summary_prompt = (
        f"用户任务：{state['user_goal']}\n"
        f"最终页面提取到的 DOM 信息：\n{state['dom_snapshot']}\n"
        f"请根据页面信息给出简明扼要的最终结果汇报。"
    )
    final_resp = await llm.ainvoke([HumanMessage(content=summary_prompt)])
    thought = f"🎉 [Critic] 所有子目标校验通过，任务圆满完成！"

    return {
        "is_completed": True,
        "final_answer": final_resp.content,
        "thought_trace": [thought],
    }

# ==================== 节点 4：Human（人机验证人工介入） ====================
async def human_intervention_node(state: AgentState) -> dict:
    """
    页面出现 CAPTCHA / Cloudflare 挑战 / 滑块时暂停自动操作：
    1. 先通过 custom 流把「等待人工验证」和当前画面推给前端；
    2. 轮询页面直到验证消失（人工在可见浏览器里完成后自动继续）；
    3. 超时则计入一次失败，交给 Planner 重新规划。
    """
    writer = get_stream_writer()

    #无头模式人工看不到界面，无法完成验证，直接给出明确指引
    if HEADLESS:
        thought = (
            "⏸ [Human] 检测到人机验证，但当前浏览器为无头模式，人工无法完成验证。"
            "请在 .env 中设置 BROWSER_HEADLESS=false 后重启服务。"
        )
        writer({"node": "human_intervention", "thought": thought, "human_verification": True})
        return {
            "last_action_result": "❌ 人机验证无法在无头模式完成，请设置 BROWSER_HEADLESS=false 后重试。",
            "needs_human_verify": False,
            "needs_replan": True,
            "error_count": state.get("error_count", 0) + 1,
            "thought_trace": [thought],
        }

    page = await get_page()
    try:
        start_shot = await browser_screenshot()
    except Exception:
        start_shot = None

    thought = (
        "⏸ [Human] 检测到人机验证（CAPTCHA/Cloudflare/滑块），Agent 已暂停。"
        "请在浏览器窗口中手动完成验证，通过后会自动继续。"
    )
    #先推一条事件，避免前端在长达数分钟的等待里毫无反馈
    writer({
        "node": "human_intervention",
        "thought": thought,
        "screenshot": start_shot,
        "human_verification": True,
    })

    async def on_progress(dom_info, elapsed_seconds, reasons):
        writer({
            "node": "human_intervention",
            "thought": (
                f"⏳ [Human] 仍在等待人工验证（已等待 {elapsed_seconds} 秒）："
                f"{verification_brief(dom_info)}"
            ),
            "human_verification": True,
        })

    result = await wait_for_human_verification(page, on_progress=on_progress)
    waited = result["waited_seconds"]

    #验证通过：重置错误计数，并让 Planner 基于最新页面重新规划剩余步骤
    if result["resolved"]:
        dom_info = result["dom_info"] or {}
        try:
            latest_shot = await browser_screenshot()
        except Exception:
            latest_shot = None
        thought = f"✅ [Human] 人机验证已通过（等待 {waited} 秒），恢复自动执行。"
        return {
            "last_action_result": (
                "✅ 人工验证已完成，页面已恢复可操作。\n"
                f"当前页面：{dom_info.get('title', '')} ({dom_info.get('url', '')})\n"
                f"{dom_info.get('distilledDOM', '')}"
            ),
            "dom_snapshot": dom_info.get("distilledDOM", ""),
            "screenshot_b64": latest_shot,
            "needs_human_verify": False,
            "needs_replan": True,
            "error_count": 0,
            "thought_trace": [thought],
        }

    #等待超时：仍无法通过验证，计入一次失败
    thought = (
        f"⌛ [Human] 等待人工验证超时（{waited} 秒），"
        f"仍检测到：{verification_brief(result['dom_info'])}"
    )
    return {
        "last_action_result": (
            f"❌ 人机验证等待超时（{waited} 秒），页面仍未放行。"
            "请人工在浏览器中完成验证后重试。"
        ),
        "needs_human_verify": False,
        "needs_replan": True,
        "error_count": state.get("error_count", 0) + 1,
        "thought_trace": [thought],
    }

# ==================== 条件边路由函数 (Conditional Edges) ====================
def critic_router(state: AgentState) -> str:
    """
    决定 Critic 校验之后的流向
    """
    # 完成全部任务 -> 结束
    if state.get("is_completed", False):
        return "end"
    # 命中人机验证 -> 转交人工介入节点
    if state.get("needs_human_verify", False):
        return "human_verify"
    #超过 MAX_ERROR_COUNT 次连续报错，强制终止避免无限消耗Token
    if state.get("error_count", 0) >= MAX_ERROR_COUNT:
        return "end"
    # 遇到弹窗遮挡或报错 -> 回到 Planner 重新规划（动态自愈核心！）
    if state.get("needs_replan", False):
        return "replan"
    # 正常推进 -> 继续让 Actor 执行下一个子步骤
    return "next_action"


def human_router(state: AgentState) -> str:
    """人工介入结束后的流向：超时且失败次数达到阈值则结束，否则回到 Planner 重规划"""
    if state.get("error_count", 0) >= MAX_ERROR_COUNT:
        return "end"
    return "replan"

# ==================== 构建并编译 StateGraph ====================
def build_browser_agent_graph(checkpointer: AsyncSqliteSaver):
    workflow = StateGraph(AgentState)

    #注册核心智能体节点（含人机验证人工介入节点）
    workflow.add_node("planner", planner_node)
    workflow.add_node("actor", actor_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("human_intervention", human_intervention_node)

    #设置入口与普通边
    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "actor")
    workflow.add_edge("actor", "critic")

    #设置Critic的条件边（自愈循环 + 人机验证转人工的关键）
    workflow.add_conditional_edges(
        "critic",
        critic_router,
        {
            "replan": "planner",                # 异常自愈：回退给 Planner
            "next_action": "actor",             # 正常步进：继续调用 Actor
            "human_verify": "human_intervention",  # 人机验证：暂停等待人工
            "end": END,                         # 任务完成或熔断：结束图循环
        },
    )

    #人工验证结束后：通过则回 Planner 重新规划剩余步骤，超时熔断则结束
    workflow.add_conditional_edges(
        "human_intervention",
        human_router,
        {
            "replan": "planner",
            "end": END,
        },
    )

    #挂载 SQLiteSaver 实现跨步断点保存与历史轨迹的回放
    return workflow.compile(checkpointer=checkpointer)
