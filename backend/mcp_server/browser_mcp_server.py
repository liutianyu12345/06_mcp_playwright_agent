import base64
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, Page

#1. 初始化标准 MCP Server
mcp = FastMCP("Playwright-Browser-MCP-Server")

# 全局维护浏览器实例（实际生产可按 session_id 隔离）
_playwright = None
_browser: Optional[Browser] = None
_page: Optional[Page] = None

DISTILL_JS_PATH = Path(__file__).parent / "dom_distiller.js"

async def get_page() -> Page:
    """懒加载获取或初始化无头浏览器页面"""
    global _playwright, _browser, _page
    if _page is None:
        _playwright = await async_playwright().start()
        # headless = True 为无头模式;调试时可改为False 亲眼看浏览器自己动！
        _browser = await _playwright.chromium.launch(headless=True)
        context = await _browser.new_context(viewport={"width": 1280, "height": 800})
        _page = await context.new_page()
    return _page

async def distill_current_page(page: Page) -> dict:
    """注入JS脚本，执行DOM树蒸馏与Set-of-Mark 标记"""
    js_code = DISTILL_JS_PATH.read_text(encoding="utf-8")
    result = await page.evaluate(js_code)
    return result

#===================一下为暴露给大模型的标砖 MCP Tools ======================

@mcp.tool()
async def browser_navigate(url: str) -> str:
    """
    导航到指定 URL 网页，并返回精简后的可交互 DOM 树（带数字编码）。
    ：param url：完整网页地址，例如 https://www.bing.com
    """
    # 仅允许 http/https，避免 file:// 或 javascript: 等协议访问本地资源
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"❌ 拒绝访问 {url}：仅支持 http/https 完整网址"

    page = await get_page()
    try:
        await page.goto(url,wait_until="domcontentloaded",timeout=15000)
        await page.wait_for_timeout(1000) #等待动态组件渲染
        dom_info = await distill_current_page(page)
        return (
            f"✅ 成功打开网页: {dom_info['title']} ({dom_info['url']})\n"
            f"当前可视区共有 {dom_info['interactiveCount']} 个可交互元素：\n"
            f"{dom_info['distilledDOM']}"
        )
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
        #timeout 设置短一点，如果被弹窗遮挡能快速捕获异常交给 Critic 自愈
        await page.click(selector,timeout=5000)#点击元素
        await page.wait_for_timeout(1000)#等待点击效果生效
        dom_info = await distill_current_page(page)
        return (
            f"✅ 成功点击编号 [{som_id}] 元素。\n"
            f"点击后页面最新状态 ({dom_info['url']})：\n{dom_info['distilledDOM']}"
        )
    except Exception as e:
        # 故意返回详细错误（如 Element intercepts pointer events 弹窗遮挡），触发 LangGraph 自愈
        try:
            dom_info = await distill_current_page(page)
            latest_dom = dom_info['distilledDOM']
        except Exception as distill_err:
            latest_dom = f"（获取最新 DOM 失败: {distill_err}）"
        return (
            f"❌ 点击编号 [{som_id}] 失败！错误原因: {str(e)}\n"
            f"请检查是否有弹窗遮挡或元素已刷新。当前最新 DOM：\n{latest_dom}"
        )

@mcp.tool()
async def browser_scroll(direction: str) -> str:
    """
    滚动页面到指定方向。
    :param direction: 滚动方向，例如 "up"、"down"
    """
    page = await get_page()
    delta = 600 if direction == "down" else -600
    await page.mouse.wheel(0,delta)
    await page.wait_for_timeout(800)
    dom_info = await distill_current_page(page)
    return f"✅ 已向{'下' if direction == 'down' else '上'}滚动页面。\n最新 DOM：\n{dom_info['distilledDOM']}"

@mcp.tool()
async def browser_screenshot() -> str:
    """
    获取当前浏览器视口的 Base64 截图（包含红色 Set-of-Mark 数字标记框）
    """
    page = await get_page()
    await distill_current_page(page)
    png_bytes = await page.screenshot(type = "jpeg",quality=60)
    return base64.b64encode(png_bytes).decode("utf-8")

if __name__ == "__main__":
    # 可作为独立标准 stdio MCP Server 启动
    mcp.run(transport="stdio")

