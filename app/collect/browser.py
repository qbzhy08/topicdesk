"""浏览器渲染：处理前端渲染（SPA）页面和懒加载图片。

优先使用本机已安装的 Google Chrome（channel="chrome"），不需要再下载浏览器；
没有 Chrome 时退回 Playwright 自带的 Chromium（需执行 playwright install chromium）。
"""
from __future__ import annotations

import time

from .fetch import UA, FetchError

_SCROLL_JS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let y = 0;
  for (let i = 0; i < 12 && y < document.body.scrollHeight; i++) {
    y += window.innerHeight; window.scrollTo(0, y); await sleep(250);
  }
  window.scrollTo(0, 0);
}
"""


def _launch(p):
    errors = []
    for kw in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return p.chromium.launch(headless=True, **kw), kw.get("channel", "chromium")
        except Exception as e:  # 该浏览器不存在，换下一个
            errors.append(f"{kw.get('channel', 'chromium')}: {str(e).splitlines()[0][:120]}")
    raise FetchError("network", "没有可用的浏览器：" + " | ".join(errors))


def render(url: str, wait_selector: str | None = None, timeout: float = 25) -> dict:
    """返回 {html, final_url, title, browser, elapsed_ms}。"""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError as e:
        raise FetchError("network", "未安装 playwright") from e
    t0 = time.time()
    with sync_playwright() as p:
        browser, name = _launch(p)
        try:
            page = browser.new_page(user_agent=UA, viewport={"width": 1280, "height": 900},
                                    locale="zh-CN")
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except PWTimeout as e:
                raise FetchError("timeout", f"浏览器加载超时 {timeout}s", url) from e
            except Exception as e:
                raise FetchError("network", f"浏览器打开失败：{str(e)[:160]}", url) from e
            status = resp.status if resp else 0
            if status in (401, 403, 451):
                raise FetchError("blocked", f"HTTP {status}，访问受限", url, status)
            try:
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=8000)
                else:
                    page.wait_for_load_state("networkidle", timeout=4000)
            except PWTimeout:
                pass  # 部分页面长连接永远不 idle，已有内容即可
            try:
                page.evaluate(_SCROLL_JS)   # 触发懒加载
                page.wait_for_timeout(600)
            except Exception:
                pass
            return {"html": page.content(), "final_url": page.url, "title": page.title(),
                    "status": status, "browser": name,
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            browser.close()


def fetch_raw(url: str, timeout: float = 30) -> dict:
    """备用入口：用真实浏览器打开 URL，等反爬 JS 验证通过（写入 cookie）后，
    用同一浏览器会话重新请求，拿到原始响应（RSS/XML/JSON）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError("network", "未安装 playwright") from e
    t0 = time.time()
    with sync_playwright() as p:
        browser, name = _launch(p)
        try:
            ctx = browser.new_context(user_agent=UA, locale="zh-CN")
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as e:
                raise FetchError("network", f"浏览器打开失败：{str(e)[:160]}", url) from e
            body, ctype, status = b"", "", 0
            for _ in range(8):                      # 验证页通常几秒内自动跳转
                page.wait_for_timeout(1500)
                r = ctx.request.get(url, timeout=timeout * 1000)
                body, ctype, status = r.body(), r.headers.get("content-type", ""), r.status
                head = body[:3000].decode("utf-8", "ignore").lower()
                if status < 400 and ("<rss" in head or "<feed" in head or "json" in ctype
                                     or ("<html" not in head and "<!doctype html" not in head)):
                    return {"content": body, "content_type": ctype, "status": status,
                            "browser": name, "elapsed_ms": int((time.time() - t0) * 1000)}
            raise FetchError("blocked", "浏览器等待后仍停留在验证页", url, status)
        finally:
            browser.close()
