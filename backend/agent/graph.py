import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage,HumanMessage
from langgraph import StateGraph,END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.agent.state import AgentState
from backend.mcp_server.browser_mcp_server import (
    browser_navigate,
    browser_click,
    browser_fill,
    browser_scroll,
    browser_screenshot,
)

#初始化支持Function Calling 的大模型
llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("BASE_URL"),
    model_name=os.getenv("MODEL_NAME"),
    temperature=float(os.getenv("LLM_TEMPERATURE")),
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
        "注意：点击或输入时，必须传入方括号内的数字编号 som_id！"
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

        tool_fn = MCP_TOOLS[tool_name]
        action_result = await tool_fn(**tool_args)

    #动作执行后，顺手抓一张带红框标记的实时截图
    screenshot = await browser_screenshot()

    return {
        "last_action_result": action_result,
        "dom_snapshot": action_result,
        "screenshot_b64": screenshot,
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

    #情况A：检测到工具执行报错（例如弹窗遮挡、超时、选择器失效）
    if "❌" in last_res:
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

# ==================== 条件边路由函数 (Conditional Edges) ====================
def critic_router(state: AgentState) -> str:
    """
    决定 Critic 校验之后的流向
    """
    #超过3次连续报错，强制终止避免无限消耗Token
    if state.get("error_count", 0) >= 3:
        return "end"
    # 完成全部任务 -> 结束
    if state.get("is_completed", False):
        return "end"
    # 遇到弹窗遮挡或报错 -> 回到 Planner 重新规划（动态自愈核心！）
    if state.get("needs_replan", False):
        return "replan"
    # 正常推进 -> 继续让 Actor 执行下一个子步骤
    return "next_action"

# ==================== 构建并编译 StateGraph ====================
def build_browser_agent_graph():
    workflow = StateGraph(AgentState)

    #注册三个核心智能体节点
    workflow.add_node("planner", planner_node)
    workflow.add_node("actor", actor_node)
    workflow.add_node("critic", critic_node)

    #设置入口与普通边
    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "actor")
    workflow.add_edge("actor", "critic")

    #设置Critic的条件边（自愈循环的关键）
    workflow.add_conditional_edge(
        "critic",
        critic_router,
        {
            "replan": "planner",      # 异常自愈：回退给 Planner
            "next_action": "actor",   # 正常步进：继续调用 Actor
            "end": END,               # 任务完成或熔断：结束图循环
        },
    )

    #挂载 SQLiteSaver 实现跨步断点保存与历史轨迹的回放
    return workflow.compile(checkpointer=checkpointer)
