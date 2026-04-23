#!/usr/bin/env python3
"""Reproducible splash-screen generator for FryNetworks Installer.

Usage:
    python tools/generate_splash.py

Reads the original logo asset from the repo, preserves its exact aspect
ratio, centres it on a 480x260 white canvas, and writes:
    resources/frynetworks_splash.png
"""

from pathlib import Path
from PIL import Image

REPO_ROOT = Path(__file__).parent.parent
SOURCE_LOGO = REPO_ROOT / "resources" / "frynetworks_logo.png"
OUTPUT_PATH = REPO_ROOT / "resources" / "frynetworks_splash.png"

CANVAS_W = 480
CANVAS_H = 260
BG_COLOR = (255, 255, 255)


def generate_splash() -> None:
    if not SOURCE_LOGO.exists():
        raise FileNotFoundError(f"Source logo not found: {SOURCE_LOGO}")

    logo = Image.open(SOURCE_LOGO)
    src_w, src_h = logo.size
    src_mode = logo.mode
    src_aspect = src_w / src_h

    print(f"source asset path: {SOURCE_LOGO}")
    print(f"source dimensions: {src_w}x{src_h} ({src_mode})")
    print(f"source aspect ratio: {src_aspect:.6f}")

    # Scale logo to fit within canvas margins while preserving aspect ratio
    # Use a target height of 140 px (matches the 74bd0c5 commit description)
    target_h = 140
    target_w = round(target_h * src_aspect)

    # Ensure it fits within canvas
    if target_w > CANVAS_W - 40:
        target_w = CANVAS_W - 40
        target_h = round(target_w / src_aspect)

    print(f"target logo dimensions: {target_w}x{target_h}")
    print(f"target logo aspect ratio: {target_w/target_h:.6f}")
    print(f"output canvas dimensions: {CANVAS_W}x{CANVAS_H}")
    # Pixel-grid rounding means exact aspect ratio cannot be preserved to infinite
    # precision, but scaling is uniform (same factor on both axes).
    scale_w = target_w / src_w
    scale_h = target_h / src_h
    # Allow up to one pixel of rounding error relative to source size
    uniform = abs(scale_w - scale_h) < (1.0 / min(src_w, src_h))
    print(f"scale factors: x={scale_w:.6f}, y={scale_h:.6f}")
    print(f"scaling is uniform (no stretch): {uniform}")

    # Convert logo to RGB if needed
    if logo.mode in ("P", "RGBA", "LA", "L"):
        logo = logo.convert("RGBA")
        # Create white background for alpha compositing
        bg = Image.new("RGBA", logo.size, BG_COLOR + (255,))
        logo = Image.alpha_composite(bg, logo).convert("RGB")
    else:
        logo = logo.convert("RGB")

    # High-quality downscale preserving aspect ratio
    logo_resized = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # Create canvas
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)

    # Paste centred
    x = (CANVAS_W - target_w) // 2
    y = (CANVAS_H - target_h) // 2
    canvas.paste(logo_resized, (x, y))

    canvas.save(OUTPUT_PATH, "PNG")
    print(f"output path: {OUTPUT_PATH}")
    print(f"output size: {OUTPUT_PATH.stat().st_size} bytes")


if __name__ == "__main__":
    generate_splash()
