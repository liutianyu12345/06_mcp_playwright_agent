import asyncio
import json
import os
import sys
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.agent.graph import build_browser_agent_graph

load_dotenv()


def proactor_loop_factory():
    """Windows 下 Playwright 启动浏览器子进程必须用 ProactorEventLoop。
    uvicorn 带 --reload/--workers 时会默认切到 SelectorEventLoop，需要显式指定：
    uvicorn backend.main:app --reload --loop backend.main:proactor_loop_factory
    """
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.SelectorEventLoop()

app = FastAPI(title = "MCP + Playwright GUI Agent API")
# 配置 CORS 中间件，允许所有来源的请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# 数据库路径
DB_PATH = os.getenv("DB_PATH", "agent_checkpoints.sqlite")

@app.get("/api/agent/stream")
async def stream_agent_task(goal:str,session_id:str = "default_session"):
    """
    SSE 流式接口：实时推送Agent 每一步的思考链（Cot）、调用的MCP 工具及：浏览器实时截图
    """
    async def event_generator():
        #连接SQLite 持久化检查点数据库
        async with AsyncSqliteSaver.from_conn_string(DB_PATH) as memory:
            agent_app = build_browser_agent_graph(checkpointer=memory)

            #thread_id 用于标识会话，支持断点续传与轨迹回放
            config = {"configurable": {"thread_id": session_id}}
            # 初始状态
            initial_state = {
                "user_goal": goal,
                "plan_steps": [],
                "current_step_idx": 0,
                "dom_snapshot": "",
                "screenshot_b64": None,
                "last_action_result": "",
                "error_count": 0,
                "needs_replan": False,
                "needs_human_verify": False,
                "is_completed": False,
                "final_answer": "",
                "thought_trace": [],
            }
            # 同时订阅 updates（节点完成）与 custom（人工验证等待进度等即时事件）
            try:
                async for mode, event in agent_app.astream(
                    initial_state,
                    config=config,
                    stream_mode=["updates", "custom"],
                ):
                    if mode == "custom":
                        # human_intervention 节点通过 get_stream_writer() 主动推送的事件
                        payload = {
                            "node": event.get("node", "human_intervention"),
                            "thought": event.get("thought", ""),
                            "plan_steps": None,
                            "screenshot": event.get("screenshot"),
                            "final_answer": None,
                            "human_verification": event.get("human_verification"),
                        }
                        yield f"data: {json.dumps(payload,ensure_ascii=False)}\n\n"
                        continue

                    for node_name,node_output in event.items():
                        payload = {
                            "node": node_name,
                            "thought": node_output.get("thought_trace", [""])[-1],
                            "plan_steps": node_output.get("plan_steps"),
                            "screenshot": node_output.get("screenshot_b64"),
                            "final_answer": node_output.get("final_answer"),
                            # 命中人机验证时前端展示「等待人工」提示，通过后自动收起
                            "human_verification": node_output.get("needs_human_verify"),
                        }
                        #按照标准SSE格式（data：...}\n\n）推送给前端
                        yield f"data: {json.dumps(payload,ensure_ascii=False)}\n\n"
            except Exception as e:
                #把真实异常推给前端并打印到控制台，避免只剩“连接中断”这种笼统提示
                traceback.print_exc()
                error_payload = {
                    "node": "error",
                    "thought": f"❌ 后端执行异常：{type(e).__name__}: {e}",
                    "plan_steps": None,
                    "screenshot": None,
                    "final_answer": None,
                }
                yield f"data: {json.dumps(error_payload,ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/agent/history/{session_id}")
async def get_session_history(session_id:str):
    """
    历史轨迹回放接口：基于 SQLiteSaver 读取指定会话的所有历史状态快照
    """

    history_snapshots = []
    async with AsyncSqliteSaver.from_conn_string(DB_PATH) as memory:
        agent_app = build_browser_agent_graph(checkpointer=memory)
        config = {"configurable": {"thread_id": session_id}}

        async for state_snapshot in agent_app.aget_state_history(config):
            history_snapshots.append({
                "checkpoint_id": state_snapshot.config["configurable"].get("checkpoint_id"),
                "next_node": state_snapshot.next,
                "thought_trace": state_snapshot.values.get("thought_trace", []),
                "current_step_idx": state_snapshot.values.get("current_step_idx"),
            })
    return {"session_id": session_id, "history": history_snapshots}
        
