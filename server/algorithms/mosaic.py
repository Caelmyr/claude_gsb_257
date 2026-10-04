"""照片马赛克（Photomosaic）。

把目标图切成规则网格，每格填入一张平均颜色最接近的素材小图：

- 网格密度：指定列数，行数由目标图长宽比自动推出，格子永远是正方形且铺满整图，
  不会因为目标图方向（横/竖）出现半格或错位。
- 素材适配：素材尺寸、方向参差不齐时，按 cover（缩放裁剪）/ contain（完整留白）/
  stretch（拉伸）三种方式统一成瓦片尺寸；contain 支持白边、黑边、模糊延伸，
  保证任何素材都恰好填满整格，绝不留空。
- 颜色匹配：在 CIELAB 感知均匀颜色空间做欧氏距离（ΔE），比直接比 RGB 更符合
  人眼对「颜色接近」的感受；素材平均色在「缩放裁剪之后的最终瓦片」上取样，
  与肉眼实际看到的颜色一致。
- 素材数量：素材多于上限时，在 Lab 空间做最远点采样，留下颜色覆盖最广的一组。
- 重复间距：按格分配时禁止同一张素材出现在邻域内（可关），并按匹配误差从大到
  小贪心挑图、用使用次数做平手裁决，既保相似度又避免连片重复。
- 颜色贴合：blend>0 时把成品按格向目标颜色收敛，远看轮廓更准，近看仍是照片。
"""
import math
from array import array

from PIL import Image, ImageEnhance, ImageFilter

from .. import config
from . import util

FIT_MODES = ("cover", "contain", "stretch")
CONTAIN_FILLS = ("white", "black", "blur")


# ---------------------------------------------------------------------------
# 颜色空间：sRGB -> Lab（D65）
# ---------------------------------------------------------------------------
def _srgb_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb):
    """单个 (r,g,b)（0-255）转 CIELAB。用于格/瓦片平均色，调用量小。"""
    r, g, b = (_srgb_linear(c) for c in rgb[:3])
    # sRGB -> XYZ（D65）
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883
    e = 216.0 / 24389.0
    fx = x ** (1.0 / 3.0) if x > e else x / (3 * (6.0 / 29.0) ** 2) + 16.0 / 116.0
    fy = y ** (1.0 / 3.0) if y > e else y / (3 * (6.0 / 29.0) ** 2) + 16.0 / 116.0
    fz = z ** (1.0 / 3.0) if z > e else z / (3 * (6.0 / 29.0) ** 2) + 16.0 / 116.0
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def average_rgb(img):
    """整张图的平均 RGB。BOX 缩小到 1x1 等价于全图像素精确平均。"""
    return img.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))[:3]


def _dist2(a, b):
    dr = a[0] - b[0]
    dg = a[1] - b[1]
    db = a[2] - b[2]
    return dr * dr + dg * dg + db * db


# ---------------------------------------------------------------------------
# 素材 -> 瓦片：任意尺寸/方向都精确填满 tw×th
# ---------------------------------------------------------------------------
def _resize_contain_size(sw, sh, tw, th):
    s = min(tw / sw, th / sh)
    return max(1, int(round(sw * s))), max(1, int(round(sh * s)))


def _resize_cover_size(sw, sh, tw, th):
    s = max(tw / sw, th / sh)
    return max(1, int(round(sw * s))), max(1, int(round(sh * s)))


def fit_tile(img, tw, th, mode, contain_fill="white"):
    """把任意尺寸的素材做成恰好 tw×th 的瓦片，返回 RGB 图。

    cover   - 等比放大到覆盖整格，居中裁掉超出部分（无变形、不留白）。
    contain - 等比缩小到完整可见，居中放置，空白用 white/black/blur 补。
    stretch - 直接拉伸到整格（允许变形）。
    """
    sw, sh = img.size
    if mode == "stretch":
        if (sw, sh) == (tw, th):
            return img.copy() if img.mode == "RGB" else img.convert("RGB")
        return img.resize((tw, th), Image.Resampling.LANCZOS)

    if mode == "contain":
        nw, nh = _resize_contain_size(sw, sh, tw, th)
        if (nw, nh) == (tw, th):
            return img.resize((tw, th), Image.Resampling.LANCZOS)
        tile = img.resize((nw, nh), Image.Resampling.LANCZOS)
        x, y = (tw - nw) // 2, (th - nh) // 2
        if contain_fill == "blur":
            # 模糊延伸：同一张图 cover 填满后高斯模糊打底，中间清晰贴入
            bg = _cover(img, tw, th)
            radius = max(4, min(tw, th) // 6)
            bg = bg.filter(ImageFilter.GaussianBlur(radius))
            bg = ImageEnhance.Brightness(bg).enhance(0.9)
            bg.paste(tile, (x, y))
            return bg
        color = (255, 255, 255) if contain_fill == "white" else (0, 0, 0)
        canvas = Image.new("RGB", (tw, th), color)
        canvas.paste(tile, (x, y))
        return canvas

    # 默认 cover
    return _cover(img, tw, th)


def _cover(img, tw, th):
    sw, sh = img.size
    nw, nh = _resize_cover_size(sw, sh, tw, th)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    if (nw, nh) == (tw, th):
        return resized
    x = (nw - tw) // 2
    y = (nh - th) // 2
    return resized.crop((x, y, x + tw, y + th))


# ---------------------------------------------------------------------------
# 素材子集：Lab 最远点采样（颜色覆盖最大化）
# ---------------------------------------------------------------------------
def _farthest_points(labs, k):
    """从 labs 中选 k 个下标，使彼此在 Lab 空间尽量分散。"""
    n = len(labs)
    if k >= n:
        return list(range(n))
    cx = sum(p[0] for p in labs) / n
    cy = sum(p[1] for p in labs) / n
    cz = sum(p[2] for p in labs) / n
    first = min(range(n), key=lambda i: _dist2(labs[i], (cx, cy, cz)))
    chosen = [first]
    in_set = [False] * n
    in_set[first] = True
    min_dist = [_dist2(labs[i], labs[first]) for i in range(n)]
    while len(chosen) < k:
        best_i, best_d = -1, -1.0
        for i in range(n):
            if not in_set[i] and min_dist[i] > best_d:
                best_i, best_d = i, min_dist[i]
        chosen.append(best_i)
        in_set[best_i] = True
        bj = labs[best_i]
        for i in range(n):
            d = _dist2(labs[i], bj)
            if d < min_dist[i]:
                min_dist[i] = d
    return chosen


# ---------------------------------------------------------------------------
# 分配：每格选一张瓦片（带重复间距约束 + 负载均衡）
# ---------------------------------------------------------------------------
def _assign(target_labs, tile_labs, cols, rows, gap):
    n_tiles = len(tile_labs)
    n_cells = cols * rows

    # 代价矩阵（float32 紧凑存放），每格只算一次
    costs = []
    best_col = [0.0] * n_cells
    for c, lab in enumerate(target_labs):
        row = array("f", (_dist2(lab, t) for t in tile_labs))
        costs.append(row)
        best_col[c] = min(row)

    # 颜色最特殊（最小代价也最高）的格优先挑，避免被重复约束挤掉好匹配
    order = sorted(range(n_cells), key=lambda c: best_col[c], reverse=True)

    assigned = [-1] * n_cells
    counts = [0] * n_tiles
    total_err = 0.0
    g = max(0, int(gap))

    for c in order:
        y, x = divmod(c, cols)
        forbidden = set()
        if g and n_tiles > 1:
            for dy in range(-g, g + 1):
                ny = y + dy
                if not 0 <= ny < rows:
                    continue
                base = ny * cols
                for dx in range(-g, g + 1):
                    nx = x + dx
                    if 0 <= nx < cols:
                        v = assigned[base + nx]
                        if v >= 0:
                            forbidden.add(v)

        row = costs[c]
        best_key = None
        best_t = -1
        for t in range(n_tiles):
            if t in forbidden:
                continue
            key = (row[t], counts[t], t)
            if best_key is None or key < best_key:
                best_key, best_t = key, t
        if best_t < 0:
            # 约束太紧（素材太少）时放宽，保证每格都有图、绝不留空
            for t in range(n_tiles):
                key = (row[t], counts[t], t)
                if best_key is None or key < best_key:
                    best_key, best_t = key, t

        assigned[c] = best_t
        counts[best_t] += 1
        total_err += math.sqrt(best_key[0])

    return assigned, counts, total_err / n_cells


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build(target, materials, params):
    """生成照片马赛克。

    params:
      cols         网格列数（10-120），行数按目标长宽比自动计算
      tile_size    每格边长像素（会按成品最长边上限自动收紧）
      fit          cover / contain / stretch
      contain_fill white / black / blur（仅 fit=contain 生效）
      max_materials 素材上限，0 表示全部使用
      repeat_gap   同素材禁入的切比雪夫邻域半径，0 关闭
      blend        颜色贴合度 0-100（把成品按格向目标色收敛的百分比）
    返回 {image, ...meta}。
    """
    cols = int(params.get("cols", 40))
    cols = max(config.MOSAIC_COLS_MIN, min(config.MOSAIC_COLS_MAX, cols))
    req_tile = int(params.get("tile_size", 48))
    req_tile = max(16, min(96, req_tile))
    fit = params.get("fit", "cover")
    if fit not in FIT_MODES:
        raise ValueError(f"未知的缩放裁剪方式: {fit}")
    contain_fill = params.get("contain_fill", "white")
    if contain_fill not in CONTAIN_FILLS:
        raise ValueError(f"未知的留白方式: {contain_fill}")
    max_materials = max(0, int(params.get("max_materials", 200)))
    max_materials = min(max_materials, config.MOSAIC_MAX_MATERIALS)
    repeat_gap = max(0, min(3, int(params.get("repeat_gap", 1))))
    blend = max(0, min(100, int(params.get("blend", 20))))

    if not materials:
        raise ValueError("素材为空，无法生成马赛克")

    work = util.downscale_to_max(util.ensure_rgb(target), config.MAX_DIM)
    w, h = work.size
    rows = max(1, int(round(cols * h / float(w))))

    # 成品最长边受上限保护（高密度 + 大格时自动收紧格尺寸）
    tile = max(8, min(req_tile, config.MOSAIC_MAX_OUTPUT // max(cols, rows)))

    # 每格的目标平均色（BOX 精确平均）
    grid = work.resize((cols, rows), Image.Resampling.BOX)
    target_labs = [rgb_to_lab(px) for px in grid.getdata()]

    # 素材预处理：限制参与缩放的分辨率（瓦片很小，无需原图），再统一适配
    work_long = max(256, tile * 8)
    tiles = []          # [(PIL.Image 瓦片, Lab 平均色)]
    for m in materials:
        src = util.downscale_to_max(util.ensure_rgb(m), work_long)
        t = fit_tile(src, tile, tile, fit, contain_fill)
        tiles.append((t, rgb_to_lab(average_rgb(t))))

    pool_size = len(tiles)
    if max_materials and pool_size > max_materials:
        keep = _farthest_points([lab for _, lab in tiles], max_materials)
        tiles = [tiles[i] for i in keep]

    tile_labs = [lab for _, lab in tiles]
    assigned, counts, mean_err = _assign(target_labs, tile_labs, cols, rows, repeat_gap)

    out_w, out_h = cols * tile, rows * tile
    out = Image.new("RGB", (out_w, out_h))
    for c, t_idx in enumerate(assigned):
        y, x = divmod(c, cols)
        out.paste(tiles[t_idx][0], (x * tile, y * tile))

    if blend > 0:
        # 低分辨率目标色最近邻放大，与格子严格对齐，一次全局混合（与逐格混合等价）
        overlay = grid.resize(out.size, Image.Resampling.NEAREST)
        out = Image.blend(out, overlay, blend / 100.0)

    used = sum(1 for n in counts if n > 0)
    return {
        "image": out,
        "cols": cols,
        "rows": rows,
        "cells": cols * rows,
        "tile_size": tile,
        "tile_size_requested": req_tile,
        "tile_size_clamped": tile < req_tile,
        "width": out_w,
        "height": out_h,
        "fit": fit,
        "contain_fill": contain_fill,
        "blend": blend,
        "repeat_gap": repeat_gap,
        "materials_pool": pool_size,
        "materials_used": used,
        "mean_color_error": round(mean_err, 2),
    }
