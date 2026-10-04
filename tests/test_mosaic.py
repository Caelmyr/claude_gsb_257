"""照片马赛克算法测试。

运行：python tests/test_mosaic.py        （无 pytest 依赖，自动 main）
也可：python -m pytest tests/test_mosaic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from server.algorithms import mosaic  # noqa: E402


# ---------------------------------------------------------------------------
# 测试素材
# ---------------------------------------------------------------------------
def solid(color, w=60, h=40):
    return Image.new("RGB", (w, h), color)


def target_half(left=(255, 0, 0), right=(0, 0, 255), w=100, h=50):
    """左右两半分明的目标图，便于检验选砖颜色。"""
    img = Image.new("RGB", (w, h))
    for x in range(w):
        for y in range(h):
            img.putpixel((x, y), left if x < w // 2 else right)
    return img


def build(materials=None, target=None, **params):
    mats = materials if materials is not None else [solid(c) for c in (
        (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255),
    )]
    return mosaic.build_mosaic(target or target_half(), mats, params)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_output_size_and_grid():
    r = build(cols=20, cell_size=30, tint=0)
    # 目标 100x50（2:1）-> 20 列 10 行，成品 600x300
    assert r["cols"] == 20 and r["rows"] == 10
    assert r["image"].size == (600, 300)
    assert r["cells"] == 200
    assert len(r["usage"]) == 5


def test_no_blank_cells_and_tiles_cover_every_cell():
    """随机抽取成品像素与对应小砖比对：每格都必须被素材完全铺满。"""
    mats = [solid(c, w=60, h=20) for c in (  # 故意全部 3:1 横图
        (200, 30, 30), (30, 200, 30), (30, 30, 200), (210, 210, 40))]
    r = build(materials=mats, cols=12, cell_size=24, fit="cover", tint=0)
    out, cell, cols = r["image"], r["cell_size"], r["cols"]
    # 覆盖检查：任意格的四角像素都应是某张素材的颜色（无白边/黑边/空白）
    tile_colors = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (210, 210, 40)]
    for cy in range(r["rows"]):
        for cx in range(cols):
            x0, y0 = cx * cell, cy * cell
            for px, py in ((x0 + 1, y0 + 1), (x0 + cell - 2, y0 + 1),
                           (x0 + 1, y0 + cell - 2), (x0 + cell - 2, y0 + cell - 2)):
                c = out.getpixel((px, py))
                assert min(sum((a - b) ** 2 for a, b in zip(c, t)) for t in tile_colors) < 4, \
                    f"格 ({cx},{cy}) 出现未被素材覆盖的像素 {c}"


def test_color_match_picks_nearest_tile():
    r = build(cols=20, cell_size=16, fit="cover", tint=0, color_space="lab")
    out, cell, cols = r["image"], r["cell_size"], r["cols"]
    reds = blues = 0
    # 左半格应以红砖为主、右半格应以蓝砖为主（均匀化播种允许少量其他素材）
    mid_y = (r["rows"] // 2) * cell + cell // 2
    for cx in range(cols):
        c = out.getpixel((cx * cell + cell // 2, mid_y))
        if cx < cols // 2:
            if c[0] > c[2]:
                reds += 1
        else:
            if c[2] > c[0]:
                blues += 1
    assert reds >= 7, f"左半红砖占比过低：{reds}/10"
    assert blues >= 7, f"右半蓝砖占比过低：{blues}/10"


def test_fit_modes_fill_mixed_orientations():
    """横图、竖图、方图混合时，三种填充方式成品尺寸一致且无空白。"""
    mats = [solid((120, 60, 60), 120, 30), solid((60, 120, 60), 30, 120),
            solid((60, 60, 120), 50, 50)]
    for fit in ("cover", "stretch", "contain"):
        r = build(materials=mats, cols=10, cell_size=20, fit=fit, tint=0)
        assert r["image"].size == (200, 100), fit
        # 成品所有像素都不应是默认画布黑(0,0,0)：contain 背景压暗后最低也有 ~(15,7,7)
        bad = sum(1 for px in r["image"].getdata() if px == (0, 0, 0))
        assert bad == 0, f"{fit} 出现 {bad} 个纯黑空像素"


def test_contain_keeps_foreground_intact():
    """contain：素材中央区域应是原色（完整不裁），四周是其模糊垫底。"""
    mats = [solid((0, 180, 0), 100, 25)]  # 4:1 横图
    r = build(materials=mats, cols=4, cell_size=40, fit="contain", tint=0)
    cx, cy = r["cell_size"] // 2, r["cell_size"] // 2
    assert r["image"].getpixel((cx, cy))[1] > 150  # 前景绿色保留在中央


def test_repeat_avoid_spreads_tiles():
    """格数 >> 素材数时：avoid 应让各素材使用次数尽量均等。"""
    r_avoid = build(cols=20, cell_size=12, repeat="avoid", tint=0)
    r_allow = build(cols=20, cell_size=12, repeat="allow", tint=0)
    assert r_avoid["distinct_tiles"] == 5  # 每张素材都被用到
    # 纯最近邻会扎堆在红/蓝砖（部分素材用量为 0）
    assert min(r_allow["usage"]) == 0
    # avoid 后：没有素材被冷落，也没有素材能垄断（上限约为份额的 1.5 倍）
    share = r_avoid["cells"] / 5
    assert min(r_avoid["usage"]) >= 1
    assert max(r_avoid["usage"]) <= int(share * 1.5) + 1, r_avoid["usage"]
    # 分布的离散度应显著小于 allow
    def spread(u):
        m = sum(u) / len(u)
        return sum((x - m) ** 2 for x in u)
    assert spread(r_avoid["usage"]) < spread(r_allow["usage"])


def test_repeat_allow_is_valid():
    r = build(cols=20, cell_size=12, repeat="allow", tint=0)
    assert sum(r["usage"]) == r["cells"]
    assert r["distinct_tiles"] >= 1


def test_material_count_subset():
    mats = [solid(c) for c in (
        (255, 0, 0), (255, 2, 2), (0, 0, 255), (0, 2, 255), (0, 255, 0))]
    r = build(materials=mats, cols=10, cell_size=12, material_count=3, tint=0)
    assert r["materials_used"] == 3
    assert len(r["selected_indices"]) == 3
    # 红/蓝/绿三主色各至少应保留一个（FPS 覆盖广）
    used_colors = {tuple(mats[i].getpixel((0, 0))) for i in r["selected_indices"]}
    assert any(c[0] > 200 for c in used_colors)
    assert any(c[2] > 200 for c in used_colors)
    assert any(c[1] > 200 for c in used_colors)


def test_material_count_zero_uses_all():
    r = build(cols=10, cell_size=12, material_count=0, tint=0)
    assert r["materials_used"] == 5


def test_deterministic():
    """相同输入参数，结果应逐像素一致（缓存依赖确定性）。"""
    a = build(cols=16, cell_size=14, tint=10)
    b = build(cols=16, cell_size=14, tint=10)
    assert list(a["image"].getdata()) == list(b["image"].getdata())


def test_param_clamping():
    r = build(cols=99999, cell_size=99999, tint=500, fit="nonsense",
              color_space="nonsense", repeat="nonsense")
    assert r["cols"] == mosaic.MAX_COLS
    assert r["fit"] == "cover" and r["color_space"] == "lab" and r["repeat"] == "avoid"
    assert r["tint"] == 100
    # 成品最长边受 MAX_OUTPUT_DIM 约束
    assert max(r["image"].size) <= mosaic.MAX_OUTPUT_DIM


def test_tint_blends_target():
    no_tint = build(cols=16, cell_size=14, tint=0)
    full_tint = build(cols=16, cell_size=14, tint=100)
    # tint=100 时成品应等于「目标图放大版」
    target = target_half().resize(full_tint["image"].size, Image.Resampling.LANCZOS)
    diffs = [abs(a - b) for a, b in zip(full_tint["image"].getpixel((2, 2)),
                                        target.getpixel((2, 2)))]
    assert max(diffs) <= 2, diffs
    assert no_tint["image"].tobytes() != full_tint["image"].tobytes()


def test_portrait_target_rows_gt_cols():
    t = Image.new("RGB", (50, 100))  # 竖图 1:2
    r = build(target=t, cols=20, cell_size=10, tint=0)
    assert r["rows"] == 40 and r["image"].size == (200, 400)


def test_empty_materials_raises():
    try:
        mosaic.build_mosaic(target_half(), [], {"cols": 10})
    except ValueError:
        return
    raise AssertionError("空素材应抛 ValueError")


def test_rgb_color_space_runs():
    r = build(cols=10, cell_size=12, color_space="rgb", tint=0)
    assert r["image"].size[0] == 120


# ---------------------------------------------------------------------------
# 无 pytest 时的独立运行入口
# ---------------------------------------------------------------------------
def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ✔ {fn.__name__}")
        passed += 1
    print(f"\n{passed} 个用例全部通过")


if __name__ == "__main__":
    _main()
