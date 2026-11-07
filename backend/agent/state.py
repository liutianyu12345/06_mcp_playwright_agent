from typing import TypedDict, List, Optional, Annotated
import operator

class AgentState(TypedDict):
    user_goal: str #用户最初的自然语言总任务
    plan_steps: List[str] #Planner 拆解出的子目标列表
    current_step_idx: int #当前正在执行第几个子目标
    dom_snapshot: str #当前网页蒸馏后的简化DOM树文本
    screenshot_b64: Optional[str] #当前页面带SoM编号的实时截图
    last_action_result: str #上一步MCP工具返回的结果或报错信息
    error_count: int #连续失败次数（防止死循环）
    needs_replan: bool #Critic 判定是否需要打回 Planner 重规划
    needs_human_verify: bool #页面命中人机验证，需要暂停并交给人工处理
    is_completed: bool #整个任务是否已完成
    final_answer: str #最终汇报给用户的采集结果
    thought_trace: Annotated[List[str],operator.add] #累加记录每一步的思考链（CoT）供前端展示
