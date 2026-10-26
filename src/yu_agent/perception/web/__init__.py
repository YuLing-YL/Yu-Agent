"""网页感知：DOM 布局 + 无障碍树 + 截图 OCR 的三模态融合。

为什么单独建一个 `web/` 子包（不并进现有的 a11y.py）：
现有 `perception/a11y.py` 走的是 **macOS 原生 AXUIElement**，那是"操作系统告诉我们的界面"；
网页里的 DOM 布局（z-index、遮挡、裁剪、shadow DOM、iframe）在原生 AX 里根本不存在。
两套信号源的采集方式、坐标系、失效模式完全不同，混在一个文件里必然互相污染。
所以这里按「一模态一文件 + 一个融合器」拆开，原生那条路保持原样。

分工：
- `layout.py`   DOM 布局快照（CDP DOMSnapshot）→ 候选节点 + bbox/层级/可见性/裁剪
- `axtree.py`   AXTree（CDP Accessibility.getFullAXTree）→ 语义 role/name
- `align.py`    两者对齐（backendDOMNodeId 精确匹配 + 合成节点继承 + 冲突标记）
- `visual.py`   截图像素 + OCR 校验（文字是否一致 / 是否被遮挡 / 是否置灰 / 是否空白）
- `fusion.py`   三路合并成统一目标，冲突时**视觉优先**，产出可直接喂 SOM 的元素
- `collect.py`  CDP 采集（Playwright 会话，一次往返拿全量，不逐元素 evaluate）
"""
