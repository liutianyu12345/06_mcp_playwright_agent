import asyncio
import base64
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, Page

# 读取项目根目录下的 .env（浏览器参数全部走环境变量，避免硬编码）
load_dotenv()

# 1. 初始化标准 MCP Server
mcp = FastMCP("Playwright-Browser-MCP-Server")

HEADLESS = os.getenv("BROWSER_HEADLESS", "false").strip().lower() == "true"
VIEWPORT_WIDTH = int(os.getenv("BROWSER_VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.getenv("BROWSER_VIEWPORT_HEIGHT", "800"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "15000"))
CLICK_TIMEOUT_MS = int(os.getenv("CLICK_TIMEOUT_MS", "5000"))
SCREENSHOT_QUALITY = int(os.getenv("SCREENSHOT_QUALITY", "60"))
HUMAN_WAIT_TIMEOUT_MS = int(os.getenv("HUMAN_WAIT_TIMEOUT_MS", "180000"))
HUMAN_POLL_INTERVAL_MS = int(os.getenv("HUMAN_POLL_INTERVAL_MS", "3000"))

# 工具返回结果里出现该标记时，LangGraph 会暂停自动操作并转交人工验证节点
HUMAN_VERIFY_MARKER = "🛑 需要人工验证"

# 全局维护浏览器实例（实际生产可按 session_id 隔离）
_playwright = None
_browser: Optional[Browser] = None
_page: Optional[Page] = None

DISTILL_JS_PATH = Path(__file__).parent / "dom_distiller.js"


async def reset_browser() -> None:
    """关闭并重置全局浏览器实例（人工关闭窗口或浏览器崩溃后调用）"""
    global _playwright, _browser, _page
    browser, playwright = _browser, _playwright
    _browser = None
    _page = None
    _playwright = None
    try:
        if browser is not None:
            await browser.close()
    except Exception:
        pass
    try:
        if playwright is not None:
            await playwright.stop()
    except Exception:
        pass


async def get_page() -> Page:
    """懒加载获取浏览器页面；页面被关闭或浏览器断开时自动重建，避免 TargetClosedError"""
    global _playwright, _browser, _page

    # 人工关闭浏览器窗口 / 崩溃后，Playwright 仍持有已失效的 page 对象
    if _page is not None and _page.is_closed():
        await reset_browser()
    if _browser is not None and not _browser.is_connected():
        await reset_browser()

    if _page is None:
        if _playwright is None:
            _playwright = await async_playwright().start()
        if _browser is None:
            _browser = await _playwright.chromium.launch(headless=HEADLESS)
        context = await _browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
        )
        _page = await context.new_page()
    return _page


async def distill_current_page(page: Page) -> dict:
    """注入JS脚本，执行DOM树蒸馏与Set-of-Mark 标记"""
    js_code = DISTILL_JS_PATH.read_text(encoding="utf-8")
    result = await page.evaluate(js_code)
    return result


def verification_brief(dom_info: Optional[dict]) -> str:
    """把验证识别结果压缩成一句可读的原因描述"""
    reasons = (dom_info or {}).get("verification", {}).get("reasons") or []
    return "；".join(reasons[:3]) if reasons else "页面出现验证组件"


def append_verify_hint(text: str, dom_info: Optional[dict]) -> str:
    """若页面命中人机验证，在工具结果后追加统一标记，交给 Agent 暂停处理"""
    if (dom_info or {}).get("verification", {}).get("detected"):
        return (
            f"{text}\n\n{HUMAN_VERIFY_MARKER}：{verification_brief(dom_info)}。"
            f"请人工在浏览器窗口中完成验证，Agent 会自动等待并继续。"
        )
    return text


async def wait_for_human_verification(
    page: Page,
    timeout_ms: Optional[int] = None,
    on_progress=None,
) -> dict:
    """轮询等待人工完成人机验证。

    返回 {"resolved": bool, "dom_info": dict|None, "waited_seconds": int, "reasons": [..]}
    on_progress(dom_info, elapsed_seconds, reasons) 用于向 SSE 推送等待进度。
    """
    timeout_ms = timeout_ms or HUMAN_WAIT_TIMEOUT_MS
    poll_interval = max(HUMAN_POLL_INTERVAL_MS, 500) / 1000
    started = time.monotonic()
    last_dom_info = None
    last_report_at = 0
    reasons = []

    while True:
        elapsed = time.monotonic() - started

        # 页面被关闭时无法继续等待
        if page.is_closed():
            return {
                "resolved": False,
                "dom_info": last_dom_info,
                "waited_seconds": int(elapsed),
                "reasons": ["浏览器页面已被关闭"],
            }

        try:
            last_dom_info = await distill_current_page(page)
            reasons = last_dom_info.get("verification", {}).get("reasons") or []
            detected = bool(last_dom_info.get("verification", {}).get("detected"))
        except Exception as exc:
            # 验证过程中页面可能正在跳转，evaluate 会短暂失败，继续轮询即可
            detected = True
            reasons = [f"页面状态读取失败: {exc}"]

        if not detected:
            return {
                "resolved": True,
                "dom_info": last_dom_info,
                "waited_seconds": int(elapsed),
                "reasons": [],
            }

        if elapsed * 1000 >= timeout_ms:
            return {
                "resolved": False,
                "dom_info": last_dom_info,
                "waited_seconds": int(elapsed),
                "reasons": reasons,
            }

        # 每 10 秒回调一次进度，避免 SSE 长时间静默
        if on_progress and elapsed - last_report_at >= 10:
            last_report_at = elapsed
            await on_progress(last_dom_info, int(elapsed), reasons)

        await asyncio.sleep(poll_interval)


# ==================== 以下为暴露给大模型的标准 MCP Tools ======================

@mcp.tool()
async def browser_navigate(url: str) -> str:
    """
    导航到指定 URL 网页，并返回精简后的可交互 DOM 树（带数字编码）。
    :param url: 完整网页地址，例如 https://www.bing.com
    """
    # 仅允许 http/https，避免 file:// 或 javascript: 等协议访问本地资源
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"❌ 拒绝访问 {url}：仅支持 http/https 完整网址"

    page = await get_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(1000)  # 等待动态组件渲染
        dom_info = await distill_current_page(page)
        text = (
            f"✅ 成功打开网页: {dom_info['title']} ({dom_info['url']})\n"
            f"当前可视区共有 {dom_info['interactiveCount']} 个可交互元素：\n"
            f"{dom_info['distilledDOM']}"
        )
        return append_verify_hint(text, dom_info)
    except Exception as e:
        return f"❌ 导航到 {url} 时出错: {str(e)}"


@mcp.tool()
async def browser_click(som_id: int) -> str:
    """
    根据 Set-of-Mark 数字编号点击页面上的元素。
    :param som_id: DOM 蒸馏列表中方括号里的数字编号，例如 [3] 则传入 3
    """
    page = await get_page()
    selector = f'[data-mcp-id="{som_id}"]'
    try:
        # timeout 设置短一点，如果被弹窗遮挡能快速捕获异常交给 Critic 自愈
        await page.click(selector, timeout=CLICK_TIMEOUT_MS)
        await page.wait_for_timeout(1000)  # 等待点击效果生效
        dom_info = await distill_current_page(page)
        text = (
            f"✅ 成功点击编号 [{som_id}] 元素。\n"
            f"点击后页面最新状态 ({dom_info['url']})：\n{dom_info['distilledDOM']}"
        )
        return append_verify_hint(text, dom_info)
    except Exception as e:
        # 故意返回详细错误（如 Element intercepts pointer events 弹窗遮挡），触发 LangGraph 自愈
        try:
            dom_info = await distill_current_page(page)
            latest_dom = dom_info['distilledDOM']
        except Exception as distill_err:
            dom_info = None
            latest_dom = f"（获取最新 DOM 失败: {distill_err}）"
        text = (
            f"❌ 点击编号 [{som_id}] 失败！错误原因: {str(e)}\n"
            f"请检查是否有弹窗遮挡或元素已刷新。当前最新 DOM：\n{latest_dom}"
        )
        return append_verify_hint(text, dom_info)


@mcp.tool()
async def browser_fill(som_id: int, text: str, press_enter: bool = True) -> str:
    """
    在指定编号的输入框中输入文本。
    :param som_id: 输入框的数字编号
    :param text: 要输入的文字内容
    :param press_enter: 输入后是否直接按回车键提交
    """
    page = await get_page()
    selector = f'[data-mcp-id="{som_id}"]'
    try:
        await page.fill(selector, text, timeout=CLICK_TIMEOUT_MS)
        if press_enter:
            await page.press(selector, "Enter")
        await page.wait_for_timeout(1200)
        dom_info = await distill_current_page(page)
        text_result = (
            f"✅ 已在 [{som_id}] 输入 '{text}'。\n"
            f"当前页面最新 DOM：\n{dom_info['distilledDOM']}"
        )
        return append_verify_hint(text_result, dom_info)
    except Exception as e:
        return f"❌ 输入失败！错误原因: {str(e)}"


@mcp.tool()
async def browser_scroll(direction: str) -> str:
    """
    滚动页面到指定方向。
    :param direction: 滚动方向，例如 "up"、"down"
    """
    page = await get_page()
    delta = 600 if direction == "down" else -600
    try:
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(800)
        dom_info = await distill_current_page(page)
        text = f"✅ 已向{'下' if direction == 'down' else '上'}滚动页面。\n最新 DOM：\n{dom_info['distilledDOM']}"
        return append_verify_hint(text, dom_info)
    except Exception as e:
        return f"❌ 滚动页面失败！错误原因: {str(e)}"


@mcp.tool()
async def browser_wait_for_human(timeout_seconds: int = 0) -> str:
    """
    当页面出现人机验证（CAPTCHA / Cloudflare 挑战 / 滑块）时，暂停自动操作并等待人工完成验证。
    :param timeout_seconds: 最长等待秒数，0 表示使用环境变量 HUMAN_WAIT_TIMEOUT_MS（默认 180 秒）
    """
    page = await get_page()
    if HEADLESS:
        return (
            "❌ 当前浏览器为无头模式(headless=true)，人工无法在界面上完成验证。"
            "请在 .env 中设置 BROWSER_HEADLESS=false 并重启服务后重试。"
        )

    timeout_ms = timeout_seconds * 1000 if timeout_seconds > 0 else HUMAN_WAIT_TIMEOUT_MS
    result = await wait_for_human_verification(page, timeout_ms=timeout_ms)
    dom_info = result["dom_info"]

    if result["resolved"] and dom_info:
        return (
            f"✅ 人工验证已完成（等待 {result['waited_seconds']} 秒）。\n"
            f"当前页面：{dom_info['title']} ({dom_info['url']})\n"
            f"{dom_info['distilledDOM']}"
        )
    return (
        f"❌ 已等待 {result['waited_seconds']} 秒仍未通过人机验证"
        f"（{'；'.join(result['reasons'][:3]) or '未知原因'}）。请人工完成后重试。"
    )


@mcp.tool()
async def browser_screenshot() -> str:
    """
    获取当前浏览器视口的 Base64 截图（包含红色 Set-of-Mark 数字标记框）
    """
    page = await get_page()
    await distill_current_page(page)
    png_bytes = await page.screenshot(type="jpeg", quality=SCREENSHOT_QUALITY)
    return base64.b64encode(png_bytes).decode("utf-8")


if __name__ == "__main__":
    # 可作为独立标准 stdio MCP Server 启动
    mcp.run(transport="stdio")
