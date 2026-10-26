"""内容边界检测：用截图像素反过来校准 AX（辅助功能）给的元素矩形。

## 为什么需要这一层

AX 报出来的 rect 只是「元素的布局盒子」，不是「你眼睛看到的那个东西」：

- 列表/表格的 `AXRow`、`AXCell` 往往比一行的文字高出 2~4 倍（盒子含 padding、
  甚至含整块的滚动可视区），画出来的框会盖住上下一整片；
- 微信/访达这类自绘或半自绘界面，容器元素常常报「整行宽 × 一整块高」，
  框里其实什么也没有；
- 少数情况下元素本身已经不可见（窗口被切走、列表已刷新），AX 仍报旧坐标，
  于是标注图上就在空白处冒出一个大红框。

模型看到的是「一堆互相重叠、还常常罩在空白上」的框，编号自然指不准。

## 做法（通用，不针对任何 App）

对每个元素的矩形，在**原图**（不含标注）里做一次梯度分析：

1. 逐行的水平梯度均值 → 文字/图标所在的行会明显高出一截；
2. 逐列的垂直梯度均值 → 同理得到内容所在的列；
3. 取首尾超阈值的行/列 → 内容的真实外接框。

于是：
- 框比内容大 → 收缩到内容（列表行从 80px 高收到 20px，不再压住邻居）；
- 框里什么都没有 → `has_content=False`，上层直接丢弃这个元素。

不依赖 numpy 也能跑（降级为不做校准），所以不会给项目加硬依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image

# 内容框相对元素框的最小保留：只在「是否收缩」时参考
_EMPTY_FILL = 0.04
# 强边缘阈值（灰度差）：超过它才算「这里真的有笔画/图标边界」。
# 用「计数」而不是「均值」是踩过坑的：600px 宽的聊天行里文字只占左边 60px，
# 对整行取均值会被右边的纯白稀释到 0，结果把有内容的行判成空框。
_EDGE_DIFF = 12
# 一行(列)里至少要有这么多个强边缘像素，才算内容。
# 取 3 而不是 2：列表行之间那条 1px 分隔线，在每一列只贡献「上边+下边」两个边缘，
# 阈值 2 会让分隔线被当成内容，把内容框横向拉到整个窗口宽。
_MIN_EDGE_COUNT = 3
_ENERGY_RATIO = 0.12
# 收缩后的框不小于这个尺寸（再小就点不准了）
_MIN_SIDE = 8


@dataclass
class ContentInfo:
    """一个元素矩形的内容分析结果。"""

    # 内容外接框（图像像素，绝对坐标）；没检测到内容时为 None
    bbox: Optional[Tuple[float, float, float, float]] = None
    # 内容面积 / 元素面积
    fill: float = 0.0
    # 是否检测到可辨识内容
    has_content: bool = False


def _numpy():
    try:
        import numpy  # noqa: WPS433 (延迟导入，未装时降级)

        return numpy
    except Exception:
        return None


def content_info(
    img: Image.Image,
    rect: Tuple[float, float, float, float],
    *,
    energy_ratio: float = _ENERGY_RATIO,
) -> Optional[ContentInfo]:
    """分析 rect 内是否真有内容，并返回内容外接框。

    rect 是**图像像素**坐标 (x0, y0, x1, y1)，必须在图像范围内。
    返回 None 表示无法分析（图太小 / 没装 numpy）—— 调用方应保持原样。
    """
    x0, y0, x1, y1 = (int(round(v)) for v in rect)
    W, H = img.size
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 - x0 < 6 or y1 - y0 < 4:
        return None

    np = _numpy()
    if np is None:
        return None

    # 收缩 1px 再分析：把「元素自身的描边/圆角」排除掉一点，避免整块被判成内容
    crop = img.crop((x0, y0, x1, y1)).convert("L")
    a = np.asarray(crop, dtype=np.float32)
    h, w = a.shape
    if h < 4 or w < 4:
        return None

    # 行能量 = 该行内「强边缘像素」的个数（文字行多，空白行 0）
    gx = np.abs(np.diff(a, axis=1)) >= _EDGE_DIFF
    gy = np.abs(np.diff(a, axis=0)) >= _EDGE_DIFF
    row_e = gx.sum(axis=1)
    col_e = gy.sum(axis=0)

    def _span(energy):
        peak = int(energy.max()) if energy.size else 0
        if peak < _MIN_EDGE_COUNT:
            return None                     # 整片都是平色 → 没有内容
        thr = max(_MIN_EDGE_COUNT, peak * energy_ratio)
        idx = [i for i, v in enumerate(energy) if float(v) >= thr]
        if not idx:
            return None
        return int(idx[0]), int(idx[-1]) + 1

    rs, cs = _span(row_e), _span(col_e)
    if rs is None or cs is None:
        return ContentInfo(bbox=None, fill=0.0, has_content=False)

    bx0, by0 = x0 + cs[0], y0 + rs[0]
    bx1, by1 = x0 + cs[1], y0 + rs[1]
    if (bx1 - bx0) < 3 or (by1 - by0) < 3:
        return ContentInfo(bbox=None, fill=0.0, has_content=False)

    area = max(1.0, (x1 - x0) * (y1 - y0))
    fill = ((bx1 - bx0) * (by1 - by0)) / area
    # has_content 只看「有没有检测到边缘」，不看 fill：
    # 一个只有两三个字的列表项，内容面积占比必然很小，但它绝对不是空框。
    return ContentInfo(
        bbox=(float(bx0), float(by0), float(bx1), float(by1)),
        fill=float(fill),
        has_content=True,
    )


def shrink_box(
    rect: Tuple[float, float, float, float],
    info: Optional[ContentInfo],
    *,
    pad: float = 2.0,
    max_shrink: float = 0.97,
) -> Tuple[float, float, float, float]:
    """把元素框收缩到内容外接框（带一点留白），并保证不会收缩过头。

    max_shrink 限制「最多收缩掉多少比例的面积」。默认放得很宽（0.97），
    因为 AX 给列表行的盒子经常是内容的 3~5 倍高，不收到位就等于没修
    —— 这正是「框一个叠一个、还罩着空白」的来源。
    调用方可以对「本来就准」的角色（按钮/输入框）传更小的值来保守处理。
    """
    x0, y0, x1, y1 = rect
    if info is None or not info.has_content or not info.bbox:
        return rect

    bx0, by0, bx1, by1 = info.bbox
    bx0, by0 = bx0 - pad, by0 - pad
    bx1, by1 = bx1 + pad, by1 + pad
    # 和原框取交集
    bx0, by0 = max(x0, bx0), max(y0, by0)
    bx1, by1 = min(x1, bx1), min(y1, by1)
    if bx1 - bx0 < _MIN_SIDE or by1 - by0 < _MIN_SIDE:
        return rect

    old_area = max(1.0, (x1 - x0) * (y1 - y0))
    new_area = (bx1 - bx0) * (by1 - by0)
    if new_area / old_area < (1.0 - max_shrink):
        return rect                          # 收缩得太狠，宁可不动
    return (bx0, by0, bx1, by1)
