"""浏览器 CDP 环境：**长连接 + 缓存**的 Playwright 会话，供网页三模态感知使用。

## 为什么不每步新开浏览器

- 冷启动 Chrome 要 1~3 秒，还要重新登录/恢复页面状态；
- 每步重建 context 会丢掉 localStorage、cookie、滚动位置、已加载的在线文档；
- `playwright install` 只在首次需要，**运行时绝不能再触发安装**。

所以这里做三件事：
1. 模块级缓存把「endpoint → 会话」记住，跨步骤、跨任务复用同一个 page；
2. 页面被关掉/崩溃时才重建（检查 `page.is_closed()`）；
3. 暴露 `geometry()`：把 CDP 的「视口内 CSS 坐标」换算到「屏幕逻辑坐标」，
   少这一步，所有点击都会稳定偏几十像素。

## 坐标系（踩过坑，写清楚）

```
屏幕逻辑像素 = 窗口左上角 + 内容区偏移 + 视口内 CSS px × zoom
```
- `Browser.getWindowForTarget` / `getWindowBounds` 拿到**窗口**在屏幕上的位置；
- 内容区偏移用 `(outer - inner)` 估算：宽度差一半在左，高度差全在上（macOS 标题栏）；
- `zoom` 用「CDP 的 CSS 视口宽 / JS 的 outerWidth」近似 —— 页面放大时 CSS 视口变小，
  这个比值跟着变，比死认 devicePixelRatio 稳。
- ⚠️ 带自定义侧栏/工具栏的浏览器（Arc、Edge 竖排标签）会让这个估算偏掉。
  真实项目里必须做一次**回投验证**：用算出来的坐标去截图上对几个已知元素的名字，
  对不上就手动给 offset。`geometry()` 只给初值，不假装精确。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from PIL import Image

_SESSIONS: Dict[str, BrowserSession] = {}


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


@dataclass
class WindowGeometry:
    """窗口/内容区的几何信息（全部是屏幕逻辑像素）。"""

    window: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # x, y, w, h
    origin: Tuple[float, float] = (0.0, 0.0)   # 内容区左上角（屏幕坐标）
    zoom: float = 1.0
    inner: Tuple[float, float] = (0.0, 0.0)
    outer: Tuple[float, float] = (0.0, 0.0)


class BrowserSession:
    """一个长期存活的浏览器会话（复用 page / CDP session）。"""

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", app: str = "浏览器"):
        if not playwright_available():
            raise RuntimeError(
                "需要 playwright：pip install -e '.[browser]'，并执行一次 "
                "`playwright install chromium`（只需一次，运行时不要再装）"
            )
        self.cdp_url = cdp_url
        self.app = app
        # playwright 是可选依赖，这里刻意保持 Any：没装时下面 _ensure_page 才有值，
        # 写成 None 会让 mypy 把 self._pw.chromium 判成「None 上没有属性」。
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._geom: Optional[WindowGeometry] = None

    # -- 连接与复用 ---------------------------------------------------- #
    def _ensure_page(self):
        from playwright.sync_api import sync_playwright

        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        if self._context is None:
            self._context = (
                self._browser.contexts[0] if self._browser.contexts
                else self._browser.new_context()
            )
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._context.new_page()
        return self._page

    @property
    def page(self):
        return self._ensure_page()

    @property
    def cdp(self):
        page = self._ensure_page()
        if self._cdp is None:
            self._cdp = page.context.new_cdp_session(page)
        return self._cdp

    # -- 几何 ---------------------------------------------------------- #
    def geometry(self, refresh: bool = False) -> WindowGeometry:
        """算内容区在屏幕上的原点 + 页面缩放。"""
        if self._geom is not None and not refresh:
            return self._geom
        page = self._ensure_page()
        g = WindowGeometry()
        try:
            info = self.cdp.send("Browser.getWindowForTarget")
            bounds = info.get("bounds") or {}
            g.window = (
                float(bounds.get("left") or 0), float(bounds.get("top") or 0),
                float(bounds.get("width") or 0), float(bounds.get("height") or 0),
            )
            win_id = info.get("windowId")
            if win_id is not None:
                b2 = self.cdp.send("Browser.getWindowBounds", {"windowId": win_id})
                b = b2.get("bounds") or {}
                if b.get("left") is not None:
                    g.window = (float(b["left"]), float(b["top"]),
                                float(b.get("width") or g.window[2]),
                                float(b.get("height") or g.window[3]))
            metrics = self.cdp.send("Page.getLayoutMetrics")
            vp = metrics.get("cssLayoutViewport") or {}
            g.inner = (float(vp.get("clientWidth") or 0), float(vp.get("clientHeight") or 0))
            js = page.evaluate(
                """() => ({
                    ox: window.outerWidth, oy: window.outerHeight,
                    sx: window.screenX, sy: window.screenY,
                })"""
            )
            g.outer = (float(js["ox"]), float(js["oy"]))
            if g.outer[0] > 0 and g.inner[0] > 0:
                g.zoom = round(max(0.25, min(4.0, g.inner[0] / g.outer[0])), 4)
            side = max(0.0, (g.outer[0] - g.inner[0]) / 2)
            chrome_h = max(0.0, g.outer[1] - g.inner[1] - side)
            g.origin = (g.window[0] + side, g.window[1] + chrome_h)
        except Exception:
            try:
                js = page.evaluate("() => ({sx: window.screenX, sy: window.screenY})")
                g.origin = (float(js["sx"]), float(js["sy"]) + 80.0)
            except Exception:
                pass
        self._geom = g
        return g

    # -- 感知 ---------------------------------------------------------- #
    def screenshot(self) -> Image.Image:
        import io

        data = self.page.screenshot(type="png")
        with Image.open(io.BytesIO(data)) as im:
            return im.convert("RGB")

    def perceive(self, *, ocr: bool = True, image_scale: float = 1.0,
                 origin: Optional[Tuple[float, float]] = None):
        """跑一次三模态感知，返回 `perception.web.collect.WebPerception`。

        origin: 手动覆盖内容区原点（窗口有自定义工具栏时用）。
        """
        from ..perception.web.collect import perceive as perceive_web

        g = self.geometry()
        return perceive_web(
            self.page,
            origin=origin or g.origin, zoom=g.zoom,
            image_scale=image_scale, app=self.app, ocr=ocr,
        )

    # -- 生命周期 ------------------------------------------------------ #
    def close(self) -> None:
        try:
            if self._cdp is not None:
                self._cdp.detach()
        except Exception:
            pass
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = self._cdp = None


def get_session(cdp_url: str = "http://127.0.0.1:9222", app: str = "浏览器") -> BrowserSession:
    """取（或建）一个缓存的会话：**同一 endpoint 永远复用同一个浏览器实例**。"""
    key = cdp_url.rstrip("/")
    sess = _SESSIONS.get(key)
    if sess is None:
        sess = BrowserSession(key, app=app)
        _SESSIONS[key] = sess
    return sess


def close_all() -> None:
    for sess in list(_SESSIONS.values()):
        sess.close()
    _SESSIONS.clear()


class BrowserCDPEnvironment:
    """兼容旧名字的薄封装（早期是个 NotImplementedError 桩）。"""

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222"):
        self.session = get_session(cdp_url)

    def capture(self) -> Image.Image:
        return self.session.screenshot()

    def screen_size(self) -> Tuple[int, int]:
        g = self.session.geometry()
        return (int(g.inner[0]), int(g.inner[1]))

    def close(self) -> None:
        self.session.close()
