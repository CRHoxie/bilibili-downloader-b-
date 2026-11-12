"""生成应用图标：B 站小电视 + 中间向下箭头。

输出 assets/app.ico（多尺寸）与 assets/icon.png（预览用）。
用高位图绘制后降采样，边缘更干净。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024  # 绘制基准尺寸
ASSETS = Path(__file__).resolve().parent / "assets"
ASSETS.mkdir(exist_ok=True)

PINK_TOP = (255, 146, 176)
PINK_BOTTOM = (216, 62, 110)
WHITE = (255, 255, 255)


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        px[0, y] = lerp(top, bottom, y / max(1, size - 1))
    return img.resize((size, size))


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def build() -> Image.Image:
    # 底部柔和投影，贴浅色背景时轮廓不糊
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [int(S * 0.035), int(S * 0.055), int(S * 0.965), int(S * 0.985)],
        radius=int(S * 0.225),
        fill=(120, 20, 60, 110),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.022))

    # 底板：粉色渐变圆角方块
    body_bg = vertical_gradient(S, PINK_TOP, PINK_BOTTOM).convert("RGBA")
    body_bg.putalpha(rounded_mask(S, int(S * 0.235)))
    base = Image.alpha_composite(shadow, body_bg)

    # 半透明高光单独一层（ImageDraw 不会做 alpha 混合，必须自己合成）
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        [int(-S * 0.25), int(-S * 0.42), int(S * 1.25), int(S * 0.46)],
        fill=(255, 255, 255, 56),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.06))
    glow.putalpha(Image.composite(glow.getchannel("A"), Image.new("L", (S, S), 0), rounded_mask(S, int(S * 0.235))))
    base = Image.alpha_composite(base, glow)

    d = ImageDraw.Draw(base)

    # ---- 小电视天线
    ant_w = int(S * 0.058)
    for sx, sy, ex, ey in [
        (0.355, 0.335, 0.245, 0.135),
        (0.645, 0.335, 0.755, 0.135),
    ]:
        d.line(
            [(int(S * sx), int(S * sy)), (int(S * ex), int(S * ey))],
            fill=WHITE,
            width=ant_w,
        )
        r = ant_w // 2
        d.ellipse(
            [int(S * ex) - r, int(S * ey) - r, int(S * ex) + r, int(S * ey) + r],
            fill=WHITE,
        )

    # ---- 电视机身：白色描边圆角矩形（略宽于高，像电视）
    left, top_y, right, bottom_y = (
        int(S * 0.145),
        int(S * 0.315),
        int(S * 0.855),
        int(S * 0.815),
    )
    stroke = int(S * 0.062)
    d.rounded_rectangle(
        [left, top_y, right, bottom_y],
        radius=int(S * 0.13),
        outline=WHITE,
        width=stroke,
    )

    # ---- 中间的下箭头（不透明，压在描边内侧）
    cx = S // 2
    shaft_w = int(S * 0.098)
    shaft_top = int(S * 0.445)
    shaft_bottom = int(S * 0.605)
    head_w = int(S * 0.285)
    head_bottom = int(S * 0.695)

    # 只有顶端收圆，底端保持直角并与箭头三角重叠，避免接缝处出现缺口
    d.rounded_rectangle(
        [cx - shaft_w // 2, shaft_top, cx + shaft_w // 2, shaft_bottom + int(S * 0.02)],
        radius=int(shaft_w * 0.34),
        corners=(True, True, False, False),
        fill=WHITE,
    )
    d.polygon(
        [
            (cx - head_w // 2, shaft_bottom - int(S * 0.008)),
            (cx + head_w // 2, shaft_bottom - int(S * 0.008)),
            (cx, head_bottom),
        ],
        fill=WHITE,
    )
    return base


def main() -> None:
    icon = build()
    icon.resize((512, 512), Image.LANCZOS).save(ASSETS / "icon.png")

    sizes = [256, 128, 64, 48, 40, 32, 24, 20, 16]
    frames = [icon.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[0].save(
        ASSETS / "app.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print("已生成:", ASSETS / "app.ico", ASSETS / "icon.png")


if __name__ == "__main__":
    main()
