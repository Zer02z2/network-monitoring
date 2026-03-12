"""
Cascade mode — invisible sweep-line animation matching frontend/public/art/app.js.
Interface: spawn_burst(bytes_, direction, matrix_w, matrix_h, now)
           render_frame(matrix_w, matrix_h, now) -> np.ndarray
           reset()
direction: "incoming" → line sweeps top→bottom
           "outgoing" → line sweeps bottom→top
           None/other → rects spawn immediately at random Y
"""

import math
import random

import numpy as np

# ── TUNABLE CONFIG ─────────────────────────────────────────────────────────
RECT_BASE_COUNT   = 5
RECT_SCALE        = 0.008
RECT_MAX_COUNT    = 28

RECT_MIN_W_FRAC   = 20  / 1920
RECT_MAX_W_FRAC   = 700 / 1920
RECT_MIN_H_FRAC   = 2   / 1080
RECT_MAX_H_FRAC   = 400 / 1080

RECT_LIFETIME     = 100
RECT_LIFETIME_VAR = 0.7
RECT_ALPHA_MIN    = 0.22
RECT_ALPHA_MAX    = 0.75
RED_CHANCE        = 0.78
STROKE_CHANCE     = 0.22

# px/s — speed of the invisible sweep line across the display height
RECT_SPEED        = 5000

FLASH_THRESHOLD   = 600
FLASH_ALPHA_MIN   = 0.75
FLASH_ALPHA_MAX   = 0.95
FLASH_LIFETIME    = 50
FLASH_MAX_COUNT   = 3

NEON_RED   = (255,  15,  45)
NEON_BLUE  = (  2, 212, 240)
NEON_WHITE = (255, 255, 255)
# ──────────────────────────────────────────────────────────────────────────

_rects: list[dict] = []


def reset():
    _rects.clear()


def spawn_burst(bytes_: int, direction, matrix_w: int, matrix_h: int, now: float):
    count = min(RECT_MAX_COUNT, RECT_BASE_COUNT + int(bytes_ * RECT_SCALE))

    rect_min_w = max(1, int(RECT_MIN_W_FRAC * matrix_w))
    rect_max_w = max(rect_min_w + 1, int(RECT_MAX_W_FRAC * matrix_w))
    rect_min_h = max(1, int(RECT_MIN_H_FRAC * matrix_h))
    rect_max_h = max(rect_min_h + 1, int(RECT_MAX_H_FRAC * matrix_h))

    scale_w = min(1.0, bytes_ / 2000)
    scale_h = min(1.0, bytes_ / 3000)
    max_w   = rect_min_w + scale_w * (rect_max_w - rect_min_w)
    max_h   = rect_min_h + scale_h * (rect_max_h - rect_min_h)

    if direction in ("incoming", "outgoing"):
        # Total time for line to cross the full display height at RECT_SPEED px/s
        sweep_ms = (matrix_h / RECT_SPEED) * 1000

        for i in range(count):
            t        = (i / (count - 1)) if count > 1 else 0.0  # 0..1 across sweep
            spawn_at = now + t * sweep_ms
            line_y   = t * matrix_h if direction == "incoming" else (1 - t) * matrix_h
            w = max(1, int(rect_min_w + random.random() * (max_w - rect_min_w)))
            h = max(1, int(rect_min_h + random.random() * (max_h - rect_min_h)))
            y = int(max(0, min(matrix_h - h, line_y)))
            life = RECT_LIFETIME * (1 - RECT_LIFETIME_VAR * 0.5 + random.random() * RECT_LIFETIME_VAR)
            _rects.append({
                'x': int(random.random() * max(0, matrix_w - w)),
                'y': y, 'w': w, 'h': h,
                'rgb': NEON_RED if random.random() < RED_CHANCE else NEON_BLUE,
                'alpha': RECT_ALPHA_MIN + random.random() * (RECT_ALPHA_MAX - RECT_ALPHA_MIN),
                'spawn_at': spawn_at,
                'created_at': spawn_at,
                'die_at': spawn_at + life,
                'stroke': random.random() < STROKE_CHANCE,
                'is_flash': False,
            })
    else:
        # No direction: all spawn immediately at random Y
        for _ in range(count):
            w = max(1, int(rect_min_w + random.random() * (max_w - rect_min_w)))
            h = max(1, int(rect_min_h + random.random() * (max_h - rect_min_h)))
            life = RECT_LIFETIME * (1 - RECT_LIFETIME_VAR * 0.5 + random.random() * RECT_LIFETIME_VAR)
            _rects.append({
                'x': int(random.random() * max(0, matrix_w - w)),
                'y': int(random.random() * max(0, matrix_h - h)),
                'w': w, 'h': h,
                'rgb': NEON_RED if random.random() < RED_CHANCE else NEON_BLUE,
                'alpha': RECT_ALPHA_MIN + random.random() * (RECT_ALPHA_MAX - RECT_ALPHA_MIN),
                'spawn_at': now,
                'created_at': now,
                'die_at': now + RECT_LIFETIME * (1 - RECT_LIFETIME_VAR * 0.5 + random.random() * RECT_LIFETIME_VAR),
                'stroke': random.random() < STROKE_CHANCE,
                'is_flash': False,
            })

    # White flash rects — always spawn immediately
    if bytes_ >= FLASH_THRESHOLD:
        intensity   = min(1.0, (bytes_ - FLASH_THRESHOLD) / 5000)
        flash_count = 1 + int(intensity * (FLASH_MAX_COUNT - 1))
        for _ in range(flash_count):
            fw = int(matrix_w * (0.25 + random.random() * 0.70))
            fh = int(matrix_h * (0.03 + intensity * 0.30 + random.random() * 0.12))
            fw = max(1, min(matrix_w, fw))
            fh = max(1, min(matrix_h, fh))
            fa = FLASH_ALPHA_MIN + intensity * (FLASH_ALPHA_MAX - FLASH_ALPHA_MIN) * random.random()
            _rects.append({
                'x': int(random.random() * max(0, matrix_w - fw)),
                'y': int(random.random() * max(0, matrix_h - fh)),
                'w': fw, 'h': fh,
                'rgb': NEON_WHITE, 'alpha': fa,
                'spawn_at': now,
                'created_at': now,
                'die_at': now + FLASH_LIFETIME * (0.6 + random.random() * 0.8),
                'stroke': random.random() < 0.45,
                'is_flash': True,
            })


def render_frame(matrix_w: int, matrix_h: int, now: float) -> np.ndarray:
    frame = np.zeros((matrix_h, matrix_w, 3), dtype=np.float32)

    i = 0
    while i < len(_rects):
        r = _rects[i]

        # Not yet scheduled — skip but keep
        if now < r['spawn_at']:
            i += 1
            continue

        # Expired — remove
        if now >= r['die_at']:
            _rects.pop(i)
            continue

        age      = now - r['created_at']
        lifetime = r['die_at'] - r['created_at']

        if r['is_flash']:
            t     = (age / lifetime) * math.pi * 0.5
            alpha = r['alpha'] * math.cos(t) ** 2
        else:
            hold   = lifetime * 0.15
            fade_t = 0.0 if age < hold else (age - hold) / (lifetime - hold)
            alpha  = r['alpha'] * max(0.0, 1.0 - fade_t)

        x1 = max(0, r['x'])
        y1 = max(0, r['y'])
        x2 = min(matrix_w, r['x'] + r['w'])
        y2 = min(matrix_h, r['y'] + r['h'])
        if x1 >= x2 or y1 >= y2:
            i += 1
            continue

        color = np.array(r['rgb'], dtype=np.float32)

        if r['stroke']:
            frame[y1,   x1:x2] = frame[y1,   x1:x2] * (1 - alpha) + color * alpha
            if y2 - 1 != y1:
                frame[y2-1, x1:x2] = frame[y2-1, x1:x2] * (1 - alpha) + color * alpha
            if y2 - y1 > 2:
                frame[y1+1:y2-1, x1]   = frame[y1+1:y2-1, x1]   * (1 - alpha) + color * alpha
                if x2 - 1 != x1:
                    frame[y1+1:y2-1, x2-1] = frame[y1+1:y2-1, x2-1] * (1 - alpha) + color * alpha
        else:
            frame[y1:y2, x1:x2] = frame[y1:y2, x1:x2] * (1 - alpha) + color * alpha

        i += 1

    return np.clip(frame, 0, 255).astype(np.uint8)
