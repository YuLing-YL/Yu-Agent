# 三模态融合感知（DOM + AXTree + 截图 OCR）

> 适用范围：**浏览器内的页面**。原生 macOS 应用没有 DOM，走另一条适配器（见第 2 节），
> 两条适配器输出同一个 `UIElement`，标注/编号/点击查表全部复用。

---

## 1. 诊断：现有感知层的问题分类

先分类，再动手。三类问题混在一起改，最后什么都改不好。

### 1.1 感知不准（本文件要解决的）

| # | 现象 | 根因 | 证据 |
| --- | --- | --- | --- |
| P1 | 网页里"明明有按钮却没编号" | 只用了 macOS 原生 AX，**完全没有 DOM**：浏览器只把外壳和少量元素暴露给 AX，网页内容大面积缺失 | `perception/a11y.py` 走 AXUIElement；全项目无 CDP/Playwright（`env/browser_cdp.py` 曾是 `NotImplementedError` 桩） |
| P2 | 元素被弹窗盖住，还是照点 | 没有任何"遮挡"概念：AX 只报矩形，不知道谁在上面 | `perception/a11y.py::_app_elements` 只做窗口级遮挡（`occluders`），无页内 z 序 |
| P3 | `aria-label` 与画面文字不一致时点错/找错 | 名字只有一个来源，没有交叉校验 | `som.describe_marks` 直接用 `el.title` |
| P4 | 置灰禁用元素被当成可点 | 禁用状态只看 AX 属性，不看像素 | `a11y._walk` 不看 disabled；`executor` 无禁用判断 |
| P5 | 同一元素多个语义时随机取一个 | AX 一个 DOM 节点可能对应多个 AX 节点 | `a11y` 按"角色白名单"过滤，没有"多对一"的合并逻辑 |
| P6 | OCR 只当兜底，没参与校验 | 顺序是"AX 为主 → 没有才 OCR" | `agent/anchor.py` 只在 `no marks` 时触发 |

### 1.2 执行工具（不属于本次改动，但要知道边界）

- `executor._type_text` 中文走剪贴板（已修）；
- 点击是 `pyautogui` 像素点击，网页里其实可以走 `Input.dispatchMouseEvent` 更稳（本次未改，
  因为"最小改动"原则：先修感知，再考虑换执行通道）。

### 1.3 规划 / 反思

- 规划器只吃"截图 + 编号表"，冲突信息没有回灌给模型 → 模型不知道"那个按钮被盖住了"；
- 反思模块（`agent/reflect.py`）只处理动作失败，不处理**感知冲突**。

---

## 2. 架构：双适配器 + 一个融合层

```
                  ┌─────────────── Controller（loop 主循环，ReAct）───────────────┐
                  │                                                              │
  Observation ──► │  web 适配器 ──┐                                              │
                  │  native 适配器┴─► Fusion ──► Target[] ──► SOM 编号 ──► VLM   │
                  │                                   │                          │
                  │                                   └─► conflicts ──► Reflector │
                  └──────────────────────────────────────────────────────────────┘
```

- **web 适配器**（本次实现）：DOM 布局快照 + AXTree + 截图 OCR —— 用户要求的三模态；
- **native 适配器**（已有）：macOS AX + 截图 OCR + 元素几何（几何就是它的"布局"信号，
  但不能假装那是 DOM）；
- **融合层**：只认统一的 `LayoutNode / AXNode / VisualVerdict`，不知道数据来自 CDP 还是 AXUIElement。

模块划分（一个文件只干一件事）：

| 文件 | 职责 |
| --- | --- |
| `perception/web/layout.py` | CDP `DOMSnapshot.captureSnapshot` → 候选节点（bbox/可见/裁剪/z/pointer-events/shadow） |
| `perception/web/axtree.py` | CDP `Accessibility.getFullAXTree` → 语义（role/name/disabled/ignored/backend id） |
| `perception/web/align.py` | 用 `backendDOMNodeId` 精确对齐；合成节点继承祖先；`aria-hidden`/`name_mismatch` 打标 |
| `perception/web/visual.py` | 截图 + OCR 四问：有内容吗 / 字对吗 / 被挡吗 / 灰了吗 |
| `perception/web/fusion.py` | 合并打分、**视觉优先**裁决、冲突记录、转 `UIElement` |
| `perception/web/collect.py` | CDP 采集 + 编排（纯函数 `build()` 可离线测试） |
| `env/browser_cdp.py` | 长连接 + 会话缓存（禁止每步新开浏览器 / 重复安装 playwright） |

---

## 3. 融合算法

### 3.1 对齐（三级可信度）

1. `backendDOMNodeId` 精确命中 —— 唯一可靠锚点，跨模态稳定；
2. AX 合成节点（iframe document、伪元素、表格行）没有 backend id → 沿 AX 父链找最近的带 id 祖先，
   继承几何（会偏大，但不会是 0 尺寸）；
3. 对不上就**如实标出来**（`no_ax` / `aria_hidden` / `ax_ignored`），不猜、不丢。

### 3.2 视觉校验（四个问题，各自有独立结论）

| 问题 | 方法 | 失败时的动作 |
| --- | --- | --- |
| 有内容吗 | 强边缘计数（`content_info`）+ 与**页面背景色**比差 + 内部 std | 判 `blank`，融合时淘汰 |
| 字对吗 | OCR 文字 vs **AX 名**（`text_match`）/ vs **DOM 文字**（`dom_match`） | 打标，融合时按情况改名 |
| 被挡吗 | DOM z 序（严格大于目标的 z）+ 覆盖率 ≥ 0.6；DOM 拿不到时用"空白但有名字"推断 | 判 `occluded`，淘汰 + 记冲突 |
| 灰了吗 | 区域接近纯灰（std<10 且 sat<14） | 只用于**反驳** DOM 的 disabled |

`text_match` 与 `dom_match` 必须分开：合成一个分数，就分不清"AX 名写错了"还是"渲染错了"。

### 3.3 冲突表（交给 Reflector）

| kind | 含义 | 处理 |
| --- | --- | --- |
| `occluded` | DOM 说可见，视觉判被盖住 | 淘汰该目标 + 提示模型"它被浮层挡住了，先关弹窗" |
| `blank` | DOM 说可见，截图上一片空白 | 淘汰（多半是刚被移除/还没渲染） |
| `name_mismatch` | AX 名与画面文字不一致 | 改用画面文字（用户就是这么认按钮的） |
| `disabled_conflict` | DOM/AX 说禁用，像素却清晰可读 | 保留可点性，记冲突（aria-disabled 与样式脱节） |

---

## 4. 接入方式（最小改动）

```python
from yu_agent.perception.web.collect import perceive
from yu_agent.perception.som import build_som_mapping

perc = perceive(page, origin=(0, 88), zoom=1.0, image_scale=1.0)   # CDP 三路采集 + 融合
marked, mapping, _ = build_som_mapping(
    screenshot, elements=perc.elements,     # ← 唯一新增参数：注入模式，不再枚举原生 AX
)
```

命令行现场诊断（不用模型、不用点鼠标）：

```bash
# Chrome 需以 --remote-debugging-port=9222 启动并保持窗口打开
python -m yu_agent web --shot /tmp/page.png
python -m yu_agent web --origin 0,88 --json     # 窗口有自定义工具栏时手动给原点
```

输出示例：

```
DOM 1834 节点 / AX 291 节点 → 对齐 96 个（no_ax 12，aria_hidden 3）→ 融合 78 个，淘汰 18 个，冲突 6 条
  #3   AXButton       登录                                    @(760,420) 96x36
  #7   AXTextField    邮箱                                    @(760,480) 320x40
  ! occluded: 提交 — text_missing;covered_by_z99
```

---

## 5. 测试（`tests/test_web_fusion.py`，10 项）

| 用例 | 锁住的行为 |
| --- | --- |
| `test_dom_snapshot_visibility_and_shadow` | `display:none` / 视口外的虚拟列表残留被剔除；shadow 内节点被标记 |
| `test_ax_parse_and_synthetic_inheritance` | `ignored` 保留但不作目标；合成节点从祖先继承几何锚点 |
| `test_align_flags` | `aria-hidden` 元素无 AX 节点仍被标出来；`aria-label` 与画面文字不符打标 |
| `test_visual_occlusion_and_text` | 弹窗遮挡识别；无遮挡时不误报 |
| `test_visual_text_mismatch` | AX 名与画面文字不同 → `text_match` 归零、`dom_match` 为 1 |
| `test_fusion_visual_wins_on_occlusion` | 视觉优先：遮挡目标淘汰 + 冲突记录 |
| `test_fusion_prefers_visual_name` | 采信画面文字，冲突留痕 |
| `test_fusion_disabled_conflict` | disabled 与像素不一致 → 记冲突而不是改判 |
| `test_css_to_image_coordinate_mapping` | 原点 + zoom + image_scale 三层换算 |
| `test_build_end_to_end_and_som_injection` | 三路合成 → 直接喂 SOM 编号 |

---

## 6. 风险点（都是会真出问题的，不是清单摆设）

1. **shadow DOM**：`captureSnapshot` 给的是拍平树，但**闭包 shadow root** 里的节点在部分
   Chromium 版本里拿不到；`DOM.getDocument(pierce=True)` 也要配合。表现是"组件里的按钮完全没编号"。
   → 兜底：`fusion.append_ocr_only` 把视觉独有元素补进来。
2. **iframe**：每个 frame 是独立 document，坐标相对自己的视口。必须按 `frameId` 累加父层偏移
   （`parse_snapshot(frame_offsets=…)`）；跨域 iframe 只能靠截图 + OCR。
3. **虚拟列表**：滚出视口的节点会被回收但**短时间仍留在快照里** → 按 bbox 与视口求交剔除
   （已实现）；滚动后再拍一帧即可一致，不要用旧帧的坐标。
4. **时序不同步**：DOM 快照、AX 树、截图是三次调用（实测 10~30ms 内）。动画/骨架屏期间
   三者会不一致 → 视觉优先正是为这个设计的；但**别把"视觉优先"用在滚动容器内部**，
   快速滚动时截图可能落后一帧（表现为点到了上一行）。
5. **`interestingOnly`**：Playwright `page.accessibility.snapshot()` 默认 `True`，会吃掉
   generic 容器与部分文本 → 必须用 CDP `getFullAXTree`（本实现如此）。
6. **z-index ≠ 绘制顺序**：transform / opacity 会创建新的层叠上下文，`z-index` 相同也可能有先后。
   当前只用 `z > target.z` 判遮挡（祖先都是 0，用 `>=` 会全灭），属于**保守近似**；
   要精确就用 `includePaintOrder` 的 paintOrder 字段（已采集，未参与判定）。
7. **页面缩放 / DPR**：`origin + css*zoom` 两层换算，任一错就是"稳定偏 N 像素"。
   校准方法：`python -m yu_agent web --origin x,y` 手动覆盖，配合 `--shot` 目视核对。
8. **canvas / WebGL**：DOM 里只有一块 `<canvas>`，元素级信息全无 → 只能靠 OCR + 坐标，
   这类页面"三模态"退化成"一模态 + 坐标"，要如实告诉上层（`meta.ocr_backend` 为空时尤其）。
9. **aria-hidden**：视觉上看得见、AX 里不存在。这类元素**必须**靠 OCR 补名字，
   否则模型看到的是一个没有标签的框。
10. **backendNodeId 生命周期**：页面重载/导航后会变，不能跨帧缓存成"永久 id"。
