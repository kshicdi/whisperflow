"""
10K 팔로워 축하 GIF 생성 스크립트.
Usage: python make_10k_gif.py
출력: ~/Desktop/10k_thanks.gif
"""
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 600, 600
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
OUT_PATH = str(Path.home() / "Desktop" / "10k_thanks.gif")

TOTAL_FRAMES = 72  # 72 frames = ~3.6s at 50ms per frame
FRAME_DURATION_MS = 50

# ── 색상 팔레트 ──────────────────────────────────────────────
BG      = (5, 5, 15)
GOLD    = (255, 215, 0)
GOLD2   = (255, 180, 40)
WHITE   = (255, 255, 255)
CYAN    = (80, 220, 255)
PINK    = (255, 100, 180)


def lerp(a, b, t):
    return a + (b - a) * t


def ease_out(t):
    return 1 - (1 - t) ** 3


def ease_in_out(t):
    return t * t * (3 - 2 * t)


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


random.seed(42)
STARS = [
    (random.randint(0, W), random.randint(0, H),
     random.uniform(0.5, 2.5),
     random.uniform(0, 2 * math.pi),
     random.choice([GOLD, WHITE, CYAN, PINK]))
    for _ in range(120)
]

BURST_PARTICLES = [
    (random.uniform(0, 2 * math.pi),
     random.uniform(60, 260),
     random.choice([GOLD, GOLD2, CYAN, PINK, WHITE]),
     random.uniform(1.5, 4.0))
    for _ in range(40)
]


def draw_bg(draw, frame):
    """배경 + 방사형 그라데이션."""
    img_bg = Image.new("RGB", (W, H), BG)
    # radial glow toward center
    cx, cy = W // 2, H // 2
    t = clamp((frame - 20) / 30)
    glow_r = int(lerp(0, 220, ease_in_out(t)))
    if glow_r > 0:
        glow_layer = Image.new("RGB", (W, H), BG)
        gd = ImageDraw.Draw(glow_layer)
        for r in range(glow_r, 0, -4):
            alpha = int(lerp(0, 18, 1 - r / glow_r))
            col = tuple(min(255, c + alpha) for c in BG)
            gd.ellipse(
                [cx - r, cy - r, cx + r, cy + r],
                fill=col
            )
        img_bg = Image.blend(img_bg, glow_layer, 0.7)
    return img_bg


def draw_stars(draw, frame):
    """반짝이는 별."""
    for x, y, size, phase, color in STARS:
        brightness = 0.4 + 0.6 * (0.5 + 0.5 * math.sin(frame * 0.18 + phase))
        r = int(size * brightness)
        if r < 1:
            continue
        c = tuple(int(v * brightness) for v in color)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=c)


def draw_burst(draw, frame):
    """초반 폭죽 파티클 (frame 0~25)."""
    t = clamp(frame / 20)
    if t >= 1.0:
        return
    cx, cy = W // 2, H // 2
    for angle, dist, color, size in BURST_PARTICLES:
        r = ease_out(t) * dist
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        fade = 1.0 - t
        c = tuple(int(v * fade) for v in color)
        s = max(1, int(size * fade))
        draw.ellipse([x - s, y - s, x + s, y + s], fill=c)


def draw_rings(draw, frame):
    """확장하는 파동 링."""
    cx, cy = W // 2, H // 2
    for i in range(3):
        start_frame = i * 8
        t = clamp((frame - start_frame) / 35)
        if t <= 0:
            continue
        radius = int(ease_out(t) * 320)
        alpha = int(255 * (1 - t) * 0.6)
        if alpha < 5:
            continue
        col = CYAN if i % 2 == 0 else GOLD
        c = tuple(int(v * alpha / 255) for v in col)
        draw.arc(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=0, end=360,
            fill=c, width=max(1, int(3 * (1 - t)))
        )


def draw_glow_text(base_img, text, font, color, cy_offset):
    """글로우 텍스트를 base_img에 합성."""
    cx = W // 2
    # Glow layer
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    bbox = gd.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw // 2
    ty = cy_offset - th // 2
    # Draw wide glow
    gd.text((tx, ty), text, font=font, fill=color + (60,))
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    # Draw medium glow
    gd2 = ImageDraw.Draw(glow)
    gd2.text((tx, ty), text, font=font, fill=color + (120,))
    glow = glow.filter(ImageFilter.GaussianBlur(4))
    # Draw sharp text
    gd3 = ImageDraw.Draw(glow)
    gd3.text((tx, ty), text, font=font, fill=color + (255,))

    base_img.paste(glow, mask=glow.split()[3])
    return base_img


def make_frame(frame):
    img = draw_bg(None, frame)
    draw = ImageDraw.Draw(img)
    img = img.convert("RGBA")

    draw_stars(draw, frame)
    draw_rings(draw, frame)
    draw_burst(draw, frame)

    # ── "1만" 텍스트 ──────────────────────────────────────
    t_main = clamp((frame - 5) / 20)
    if t_main > 0:
        scale = 0.6 + 0.4 * ease_out(t_main)
        alpha = int(255 * ease_out(t_main))
        font_size = int(130 * scale)
        try:
            font_main = ImageFont.truetype(FONT_PATH, font_size, index=7)
        except Exception:
            font_main = ImageFont.truetype(FONT_PATH, font_size)

        text_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        text_img = draw_glow_text(text_img, "1만 팔로우", font_main, GOLD, 220)
        # alpha mask
        alpha_mask = text_img.split()[3].point(lambda p: int(p * alpha / 255))
        text_img.putalpha(alpha_mask)
        img.paste(text_img, mask=text_img.split()[3])

    # ── "감사합니다" 텍스트 ──────────────────────────────────
    t_sub = clamp((frame - 25) / 20)
    if t_sub > 0:
        alpha2 = int(255 * ease_out(t_sub))
        try:
            font_sub = ImageFont.truetype(FONT_PATH, 60, index=5)
        except Exception:
            font_sub = ImageFont.truetype(FONT_PATH, 60)

        text_img2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        text_img2 = draw_glow_text(text_img2, "감사합니다 🙏", font_sub, WHITE, 330)
        alpha_mask2 = text_img2.split()[3].point(lambda p: int(p * alpha2 / 255))
        text_img2.putalpha(alpha_mask2)
        img.paste(text_img2, mask=text_img2.split()[3])

    # ── 부제 텍스트 ──────────────────────────────────────────
    t_caption = clamp((frame - 40) / 15)
    if t_caption > 0:
        alpha3 = int(200 * ease_out(t_caption))
        try:
            font_cap = ImageFont.truetype(FONT_PATH, 28, index=3)
        except Exception:
            font_cap = ImageFont.truetype(FONT_PATH, 28)

        text_img3 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        text_img3 = draw_glow_text(text_img3, "항상 응원해주셔서 감사합니다 ✨", font_cap, CYAN, 420)
        alpha_mask3 = text_img3.split()[3].point(lambda p: int(p * alpha3 / 255))
        text_img3.putalpha(alpha_mask3)
        img.paste(text_img3, mask=text_img3.split()[3])

    return img.convert("RGB")


def main():
    print("GIF 생성 중...", flush=True)
    frames = []
    for i in range(TOTAL_FRAMES):
        f = make_frame(i)
        frames.append(f)
        if i % 10 == 0:
            print(f"  frame {i}/{TOTAL_FRAMES}", flush=True)

    # 마지막 10프레임은 첫 프레임으로 fade-out (부드러운 루프)
    # 단순 반복 루프로 저장
    frames[0].save(
        OUT_PATH,
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_DURATION_MS,
        loop=0,
        optimize=False,
    )
    print(f"✓ 저장 완료: {OUT_PATH}")


if __name__ == "__main__":
    main()
