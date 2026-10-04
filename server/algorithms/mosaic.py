"""照片马赛克（Photo Mosaic）。

把目标图切成规则网格，每格填入图库里「平均颜色最接近」的一张素材小图，
远看是目标图的轮廓，近看是一格一格的小照片。

关键设计：

- 目标颜色：按网格做区域平均（BOX 降采样到 cols×rows），在 CIE-Lab 空间比色
  （可选加权 RGB），比直接在 RGB 上比欧氏距离更贴近人眼的色彩感受。
- 素材预处理：每张素材按「每格填充方式」预渲染成正方形小砖——
  cover（等比缩放后居中裁剪）/ stretch（拉伸）/ contain（完整居中，
  空白处用该图自身的模糊放大版垫底）。素材方向、长宽比再杂乱也保证
  每格被铺满，不出现空格、黑边或错位。
- 选砖：默认在 Lab 空间做「带配额的误差扩散」（Floyd–Steinberg 蛇形扫描）——
  每格选色后把色差残量扩散给相邻格，相邻格会换砖补偿，既让整片平均色
  收敛到目标图，又避免同一张素材连片重复出现条带；每张砖还有用量硬上限，
  素材远少于格子时也会被均匀分散使用。repeat=allow 可退化为纯最近邻。
- material_count：可只挑少数素材参与，按颜色最远点采样（FPS）选出
  对整体色彩覆盖最广的子集，而不是简单取前 N 张。
- tint：把目标图按比例整体叠加到马赛克上，加强远看轮廓的还原度。
"""
from PIL import Image, ImageEnhance, ImageFilter

from . import util

# ---------------------------------------------------------------------------
# 限制常量
# ---------------------------------------------------------------------------
MIN_COLS = 4                 # 网格列数下限（网格密度）
MAX_COLS = 150               # 网格列数上限
MIN_CELL_SIZE = 10           # 每格输出像素下限
MAX_CELL_SIZE = 96           # 每格输出像素上限
MAX_OUTPUT_DIM = 6000        # 成品最长边上限（控制内存/文件体积）
MAX_MATERIALS = 800          # 单次参与的素材数量上限

FIT_MODES = ("cover", "stretch", "contain")

# 加权 RGB 的通道权重（和为 1，再放大 3 倍使其量级接近 Lab 距离）
_RGB_W = (0.30 * 3.0, 0.59 * 3.0, 0.11 * 3.0)


# ---------------------------------------------------------------------------
# 参数处理
# ---------------------------------------------------------------------------
def _clamp_int(value, lo, hi, default):
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _clamped(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# 颜色空间：sRGB -> CIE-Lab（D65）
# ---------------------------------------------------------------------------
def _srgb_to_linear(c):
    c = c / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _lab_f(t):
    d = 6.0 / 29.0
    if t > d ** 3:
        return t ** (1.0 / 3.0)
    return t / (3.0 * d * d) + 4.0 / 29.0


def rgb_to_lab(pixels):
    """把 [(r,g,b), ...]（0-255）批量转成 [(L,a,b), ...]。"""
    out = []
    append = out.append
    for r, g, b in pixels:
        r = _srgb_to_linear(r)
        g = _srgb_to_linear(g)
        b = _srgb_to_linear(b)
        x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
        y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
        z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
        fx = _lab_f(x / 0.95047)
        fy = _lab_f(y / 1.0)
        fz = _lab_f(z / 1.08883)
        append((116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)))
    return out


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


# ---------------------------------------------------------------------------
# 素材铺砖：三种「填满每一格」的方式
# ---------------------------------------------------------------------------
def fit_cover(img, size):
    """等比缩放使短边铺满，再居中裁剪成 size×size（无变形、无空白）。"""
    w, h = img.size
    scale = max(size / w, size / h)
    rw = max(size, int(round(w * scale)))
    rh = max(size, int(round(h * scale)))
    resized = img.resize((rw, rh), Image.Resampling.LANCZOS)
    x = (rw - size) // 2
    y = (rh - size) // 2
    return resized.crop((x, y, x + size, y + size))


def fit_stretch(img, size):
    """直接缩放到 size×size（铺满，但长宽比悬殊时会变形）。"""
    return img.resize((size, size), Image.Resampling.LANCZOS)


def fit_contain(img, size):
    """等比缩放到格内完整显示，剩余区域用该图自身的模糊放大版铺满。

    前景素材一个像素都不裁；背景是同一张图 cover 铺满后高斯模糊 + 压暗，
    因此横竖不一的素材也不会出现黑边/白边或空格。
    """
    bg = fit_cover(img, size)
    bg = bg.filter(ImageFilter.GaussianBlur(max(2.0, size * 0.12)))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)

    fg = img.copy()
    fg.thumbnail((size, size), Image.Resampling.LANCZOS)
    bg.paste(fg, ((size - fg.size[0]) // 2, (size - fg.size[1]) // 2))
    return bg


_FIT_BUILDERS = {
    "cover": fit_cover,
    "stretch": fit_stretch,
    "contain": fit_contain,
}


def _average_rgb(img, square=False):
    """整图（或中心正方形区域）的平均颜色，用 BOX 降采样到 1×1 实现。"""
    if square:
        w, h = img.size
        side = min(w, h)
        x = (w - side) // 2
        y = (h - side) // 2
        img = img.crop((x, y, x + side, y + side))
    return img.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))[:3]


# ---------------------------------------------------------------------------
# 素材数量筛选：颜色最远点采样
# ---------------------------------------------------------------------------
def select_material_indices(colors, count):
    """从素材颜色集合里选 count 个，使选出的颜色对整体覆盖最广。

    起点取最接近平均色的素材，之后每轮选离已选集合最远的一个（FPS）。
    返回索引列表（保持原始相对顺序）。count>=数量 时全部使用。
    """
    n = len(colors)
    if count <= 0 or count >= n:
        return list(range(n))

    mean = tuple(sum(c[j] for c in colors) / n for j in range(3))
    first = min(range(n), key=lambda i: _dist2(colors[i], mean))
    chosen = [first]
    chosen_set = {first}
    min_d = [_dist2(c, colors[first]) for c in colors]

    while len(chosen) < count:
        nxt = -1
        best_d = -1.0
        for i in range(n):
            if i in chosen_set:
                continue
            # 距离相同时取索引小的，保证结果确定
            if min_d[i] > best_d:
                best_d = min_d[i]
                nxt = i
        chosen.append(nxt)
        chosen_set.add(nxt)
        cn = colors[nxt]
        for i in range(n):
            d = _dist2(colors[i], cn)
            if d < min_d[i]:
                min_d[i] = d

    chosen.sort()
    return chosen


# ---------------------------------------------------------------------------
# 选砖分配
# ---------------------------------------------------------------------------
def _dist2_matrix(cell_colors, tile_colors, color_space):
    """cells×tiles 的平方距离矩阵（Lab 或加权 RGB）。"""
    wr, wg, wb = _RGB_W
    lab = color_space == "lab"
    mat = []
    for tc in cell_colors:
        t0, t1, t2 = tc
        row = []
        for c in tile_colors:
            d0, d1, d2 = c[0] - t0, c[1] - t1, c[2] - t2
            if lab:
                row.append(d0 * d0 + d1 * d1 + d2 * d2)
            else:
                row.append(wr * d0 * d0 + wg * d1 * d1 + wb * d2 * d2)
        mat.append(row)
    return mat


def _assign_tiles(cell_colors, tile_colors, cols, rows, repeat, color_space):
    """为每格选一张素材，返回 (picks, counts)。

    repeat=allow：纯最近邻（每格独立选颜色最近的砖）。
    repeat=avoid：误差扩散 + 配额封顶。
      1. 预播种：每张素材在它最匹配的空格各占一位（素材数 <= 格数时保证都出现）；
      2. Floyd–Steinberg 式误差扩散扫描：每格在「目标色 + 累积残差」上选砖，
         选完把残差按 7/16、3/16、5/16、1/16 扩散给右/左下/下/右下。
         残差会驱动相邻格去选不同的砖补偿色差，天然消除行/列连片条带，
         同时整片马赛克的平均色仍然收敛到目标图；
      3. 硬上限：任何砖用量不超过 min(counts)+share，色差极端时也不垄断；
      4. 偶数行反向扫描（serpentine），避免单向扩散偏置。
    """
    cells, m = len(cell_colors), len(tile_colors)
    d2 = _dist2_matrix(cell_colors, tile_colors, color_space)
    picks = [-1] * cells
    counts = [0] * m

    if repeat == "allow" or m == 1:
        for ci, row in enumerate(d2):
            ti = min(range(m), key=row.__getitem__)
            picks[ci] = ti
            counts[ti] += 1
        return picks, counts

    # 1) 预播种：每张素材找一个自己最匹配的空格
    order = sorted(range(m), key=lambda t: min(d2[c][t] for c in range(cells)), reverse=True)
    for t in order:
        best_c, best_d = -1, float("inf")
        for c in range(cells):
            if picks[c] != -1:
                continue
            if d2[c][t] < best_d:
                best_d, best_c = d2[c][t], c
        if best_c >= 0:
            picks[best_c] = t
            counts[t] += 1

    # 2) 蛇形误差扩散
    share = max(1.0, cells / float(m))
    residual = [[0.0, 0.0, 0.0] for _ in range(cells)]

    def diffuse(x, y, dx, dy, frac, err):
        nx, ny = x + dx, y + dy
        if 0 <= nx < cols and 0 <= ny < rows:
            j = ny * cols + nx
            if picks[j] == -1:
                for k in range(3):
                    residual[j][k] += err[k] * frac

    for y in range(rows):
        xs = range(cols) if y % 2 == 0 else range(cols - 1, -1, -1)
        for x in xs:
            c = y * cols + x
            if picks[c] != -1:
                continue
            target = cell_colors[c]
            r0 = target[0] + residual[c][0]
            r1 = target[1] + residual[c][1]
            r2 = target[2] + residual[c][2]
            cap = min(counts) + int(share) + 1

            best_t, best_d = 0, float("inf")
            for t in range(m):
                if counts[t] >= cap:
                    continue
                tc = tile_colors[t]
                e0, e1, e2 = tc[0] - r0, tc[1] - r1, tc[2] - r2
                dv = e0 * e0 + e1 * e1 + e2 * e2
                if dv < best_d:
                    best_d, best_t = dv, t
            if best_d == float("inf"):  # 全部撞上限（极端配额），放开重选
                best_t = min(range(m), key=lambda t: d2[c][t])
            picks[c] = best_t
            counts[best_t] += 1

            chosen = tile_colors[best_t]
            err = (target[0] - chosen[0], target[1] - chosen[1], target[2] - chosen[2])
            if y % 2 == 0:
                diffuse(x, y, 1, 0, 7 / 16, err)
                diffuse(x, y, -1, 1, 3 / 16, err)
                diffuse(x, y, 0, 1, 5 / 16, err)
                diffuse(x, y, 1, 1, 1 / 16, err)
            else:
                diffuse(x, y, -1, 0, 7 / 16, err)
                diffuse(x, y, 1, 1, 3 / 16, err)
                diffuse(x, y, 0, 1, 5 / 16, err)
                diffuse(x, y, -1, 1, 1 / 16, err)

    return picks, counts


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_mosaic(target, materials, params):
    """生成照片马赛克。

    :param target: 目标底样 PIL 图（任意尺寸/方向）
    :param materials: 素材 PIL 图列表（尺寸、方向可参差不齐）
    :param params: dict，键见下；非法值自动回退/钳制
        cols            网格列数（密度），4..150，默认 48
        cell_size       每格边长像素，10..96，默认 40
        fit             cover / stretch / contain，默认 cover
        material_count  实际使用的素材数，0 表示全部
        repeat          avoid（均匀分散，默认）/ allow
        color_space     lab（默认）/ rgb
        tint            0..100，目标色调叠加强度，默认 15
    :returns: dict，含 image 与统计 meta（cols/rows/cells/usage 等）

    结果完全确定：相同输入与参数必然得到同一张马赛克（便于缓存）。
    """
    if not materials:
        raise ValueError("素材图像列表为空")

    cols = _clamp_int(params.get("cols", 48), MIN_COLS, MAX_COLS, 48)
    cell = _clamp_int(params.get("cell_size", 40), MIN_CELL_SIZE, MAX_CELL_SIZE, 40)
    fit = params.get("fit", "cover")
    if fit not in FIT_MODES:
        fit = "cover"
    repeat = params.get("repeat", "avoid")
    if repeat not in ("avoid", "allow"):
        repeat = "avoid"
    color_space = params.get("color_space", "lab")
    if color_space not in ("lab", "rgb"):
        color_space = "lab"
    tint = _clamped(params.get("tint", 15), 0.0, 100.0, 15.0) / 100.0

    total_materials = len(materials)
    wanted = _clamp_int(params.get("material_count", 0), 0, total_materials, 0)

    # 1) 目标图：统一 RGB，行数由目标长宽比推导（格子为正方形，不拉伸轮廓）
    target_rgb = util.ensure_rgb(target)
    w, h = target_rgb.size
    rows = max(1, min(MAX_COLS, int(round(cols * h / float(w)))))

    # 成品最长边上限：必要时缩小每格像素（网格密度不变）
    cell = max(MIN_CELL_SIZE, min(cell, MAX_OUTPUT_DIM // cols, MAX_OUTPUT_DIM // rows))

    # 2) 素材预渲染：平均颜色 + 对应填充方式的正方形小砖
    builder = _FIT_BUILDERS[fit]
    rgb_colors, tiles = [], []
    for mat in materials:
        mat = util.ensure_rgb(mat)
        # stretch / contain 前景是整图，用整图均值；cover 主要看到中心区域，用中心方形均值
        avg = _average_rgb(mat, square=(fit == "cover"))
        rgb_colors.append(avg)
        tiles.append(builder(mat, cell))

    # 3) 按素材数量筛选（颜色最远点采样），保持原相对顺序
    pick_pool = select_material_indices(
        rgb_to_lab(rgb_colors) if color_space == "lab" else rgb_colors,
        wanted,
    ) if wanted else list(range(total_materials))
    tiles = [tiles[i] for i in pick_pool]
    rgb_colors = [rgb_colors[i] for i in pick_pool]
    tile_colors = rgb_to_lab(rgb_colors) if color_space == "lab" else rgb_colors
    m = len(tiles)

    # 4) 目标每格的平均颜色（BOX 重采样支持任意比例的大步长降采样，直接取网格尺寸）
    grid = target_rgb.resize((cols, rows), Image.Resampling.BOX).convert("RGB")
    cell_colors = list(grid.getdata())
    if color_space == "lab":
        cell_colors = rgb_to_lab(cell_colors)

    # 5) 逐格选砖
    picks, counts = _assign_tiles(cell_colors, tile_colors, cols, rows,
                                  repeat, color_space)

    # 6) 拼版：每格 paste 一张完整小砖，尺寸严丝合缝，无空格/错位
    out = Image.new("RGB", (cols * cell, rows * cell))
    for ci, ti in enumerate(picks):
        cx, cy = (ci % cols) * cell, (ci // cols) * cell
        out.paste(tiles[ti], (cx, cy))

    # 7) 可选目标色调叠加（整体一次 blend，与逐格调色等价但快得多）
    if tint > 0.0:
        overlay = target_rgb.resize(out.size, Image.Resampling.LANCZOS)
        out = Image.blend(out, overlay, tint)

    distinct = sum(1 for c in counts if c > 0)
    return {
        "image": out,
        "cols": cols,
        "rows": rows,
        "cell_size": cell,
        "width": out.size[0],
        "height": out.size[1],
        "cells": cols * rows,
        "fit": fit,
        "repeat": repeat,
        "color_space": color_space,
        "tint": round(tint * 100),
        "materials_total": total_materials,
        # material_count 是「参与筛选」的池子大小；素材多于格子时只有部分真正入图，
        # materials_used 以实际被贴上的不同素材数为准。
        "materials_used": distinct,
        "materials_pool": m,
        "distinct_tiles": distinct,
        "repeat_cells": cols * rows - distinct,
        "usage": counts,
        "selected_indices": pick_pool,
    }
