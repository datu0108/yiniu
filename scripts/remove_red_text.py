#!/usr/bin/env python3
"""去除图片上红色文字/红框水印（局部颜色修复）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image


def strong_red(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return r >= 160 and g <= 140 and b <= 140 and r >= g + 45 and r >= b + 45


def weak_red(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return r >= 85 and g <= 120 and b <= 120 and r >= g + 28 and r >= b + 28


def build_mask(img: Image.Image) -> list[list[bool]]:
    w, h = img.size
    px = img.load()
    mask = [[strong_red(px[x, y][:3]) for x in range(w)] for y in range(h)]

    xs = [x for y in range(h) for x in range(w) if mask[y][x]]
    ys = [y for y in range(h) for x in range(w) if mask[y][x]]
    if not xs:
        return mask

    pad = 18
    x0, x1 = max(0, min(xs) - pad), min(w, max(xs) + pad)
    y0, y1 = max(0, min(ys) - pad), min(h, max(ys) + pad)
    for y in range(y0, y1):
        for x in range(x0, x1):
            if weak_red(px[x, y][:3]):
                mask[y][x] = True
    return mask


def inpaint(img: Image.Image, mask: list[list[bool]], radius: int = 12) -> Image.Image:
    w, h = img.size
    px = img.load()
    out = img.copy()
    opx = out.load()

    for y in range(h):
        for x in range(w):
            if not mask[y][x]:
                continue
            rs, gs, bs, n = 0, 0, 0, 0
            for dy in range(-radius, radius + 1):
                ny = y + dy
                if ny < 0 or ny >= h:
                    continue
                for dx in range(-radius, radius + 1):
                    nx = x + dx
                    if nx < 0 or nx >= w or mask[ny][nx]:
                        continue
                    r, g, b = px[nx, ny][:3]
                    rs += r
                    gs += g
                    bs += b
                    n += 1
            if n:
                opx[x, y] = (rs // n, gs // n, bs // n, px[x, y][3] if len(px[x, y]) > 3 else 255)
    return out


def remove_red(path: Path, out: Path, dilate: int = 3) -> None:
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    mask = build_mask(img)

    for _ in range(dilate):
        nm = [row[:] for row in mask]
        for y in range(h):
            for x in range(w):
                if mask[y][x]:
                    continue
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny][nx]:
                        nm[y][x] = True
                        break
        mask = nm

    result = inpaint(img, mask, radius=18)
    # 第二遍修补边缘漏网红色
    mask2 = build_mask(result)
    if any(any(row) for row in mask2):
        result = inpaint(result, mask2, radius=14)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.convert("RGB").save(out, quality=95)
    print(f"已保存: {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="去除红色水印文字")
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--output", type=Path, default=None)
    args = p.parse_args()
    if not args.input.is_file():
        print(f"文件不存在: {args.input}", file=sys.stderr)
        sys.exit(1)
    out = args.output or args.input.with_stem(args.input.stem + "_clean")
    remove_red(args.input, out)


if __name__ == "__main__":
    main()
