// ── TUNABLE CONFIG ────────────────────────────────────────────────────────
const RECT_BASE_COUNT = 2 // rects spawned per burst regardless of size
const RECT_SCALE = 0.0012 // additional rects per byte
const RECT_MAX_COUNT = 28 // max rects per single burst
const RECT_MIN_W = 20 // px — min rect width
const RECT_MAX_W = 700 // px — max rect width
const RECT_MIN_H = 2 // px — min rect height (can be hairline thin)
const RECT_MAX_H = 400 // px — max rect height
const RECT_LIFETIME = 100 // ms — base lifetime
const RECT_LIFETIME_VAR = 0.7 // randomness factor on lifetime
const RECT_ALPHA_MIN = 0.22 // starting alpha minimum
const RECT_ALPHA_MAX = 0.75 // starting alpha maximum
const RED_CHANCE = 0.78 // probability of neon red vs neon blue
const STROKE_CHANCE = 0.22 // probability of outline-only rect

const RECT_SPEED = 10000 // px/s — speed of directional rects (incoming = down, outgoing = up)

const Y_SPREAD = 0.28 // spread around stream Y as fraction of screen height
const Y_RANDOM_CHANCE = 0.05 // probability of fully random Y
const Y_OPPOSITE_CHANCE = 0.09 // probability of spawning near opposite side of screen
const STREAM_PER_BYTE = 0.00006 // how much stream position advances per byte (0–1)

const FLASH_THRESHOLD = 600 // bytes — triggers white flash rect
const FLASH_ALPHA_MIN = 0.75
const FLASH_ALPHA_MAX = 0.95
const FLASH_LIFETIME = 50 // ms — white flash is short and brutal
const FLASH_MAX_COUNT = 3 // max white flash rects per burst

const MAX_RECTS = 350 // global cap — oldest pruned when exceeded

const COLOR_BG = "#000000"
const NEON_RED = [255, 15, 45]
const NEON_BLUE = [2, 212, 240]
const NEON_WHITE = [255, 255, 255]
// ─────────────────────────────────────────────────────────────────────────

const canvas = document.getElementById("canvas")
const ctx = canvas.getContext("2d")

// Normalized stream position 0..1 — advances with each packet, drives top→down flow
let streamY = 0.05

// All active rects: { x, y, w, h, rgb, alpha, createdAt, dieAt, stroke, isFlash, vy }
// vy > 0 = moving down (incoming from tracked IP)
// vy < 0 = moving up   (outgoing to tracked IP)
// vy = 0 = stationary  (no direction / flash)
const rects = []

let lastTick = 0

// ── Y distribution ────────────────────────────────────────────────────────
// Weighted toward streamY, with tails toward opposite side and pure random
const pickY = (baseY, h) => {
  const r = Math.random()
  let center
  if (r < Y_RANDOM_CHANCE) {
    return Math.random() * Math.max(0, canvas.height - h)
  } else if (r < Y_RANDOM_CHANCE + Y_OPPOSITE_CHANCE) {
    center = canvas.height - baseY // opposite side
  } else {
    center = baseY
  }
  // Two-uniform sum approximates Gaussian (CLT)
  const spread = Y_SPREAD * canvas.height
  const y = center + (Math.random() + Math.random() - 1) * spread
  return Math.max(0, Math.min(canvas.height - h, y))
}

// ── Spawn burst ───────────────────────────────────────────────────────────
// direction: "incoming" = rects shoot downward
//            "outgoing" = rects shoot upward
//            undefined  = stationary, fades as before
const spawnBurst = (bytes, direction) => {
  const vy =
    direction === "incoming"
      ? RECT_SPEED
      : direction === "outgoing"
        ? -RECT_SPEED
        : 0
  const baseY = streamY * canvas.height
  const count = Math.min(
    RECT_MAX_COUNT,
    RECT_BASE_COUNT + Math.floor(bytes * RECT_SCALE),
  )
  const now = performance.now()

  for (let i = 0; i < count; i++) {
    // Size scales with bytes — large packets can fill more of the screen
    const maxW =
      RECT_MIN_W + Math.min(1, bytes / 2000) * (RECT_MAX_W - RECT_MIN_W)
    const maxH =
      RECT_MIN_H + Math.min(1, bytes / 3000) * (RECT_MAX_H - RECT_MIN_H)
    const w = RECT_MIN_W + Math.random() * (maxW - RECT_MIN_W)
    const h = RECT_MIN_H + Math.random() * (maxH - RECT_MIN_H)

    const x = Math.random() * Math.max(0, canvas.width - w)
    const y = pickY(baseY, h)
    const rgb = Math.random() < RED_CHANCE ? NEON_RED : NEON_BLUE
    const alpha =
      RECT_ALPHA_MIN + Math.random() * (RECT_ALPHA_MAX - RECT_ALPHA_MIN)
    const life =
      RECT_LIFETIME *
      (1 - RECT_LIFETIME_VAR * 0.5 + Math.random() * RECT_LIFETIME_VAR)
    const stroke = Math.random() < STROKE_CHANCE
    // Moving rects die when off-screen; stationary rects die by lifetime
    const dieAt = vy !== 0 ? Infinity : now + life

    rects.push({
      x,
      y,
      w,
      h,
      rgb,
      alpha,
      createdAt: now,
      dieAt,
      stroke,
      isFlash: false,
      vy,
    })
  }

  // White flash rects for large packets — count scales with size
  if (bytes >= FLASH_THRESHOLD) {
    const intensity = Math.min(1, (bytes - FLASH_THRESHOLD) / 5000)
    const flashCount = 1 + Math.floor(intensity * (FLASH_MAX_COUNT - 1))
    for (let f = 0; f < flashCount; f++) {
      const fw = canvas.width * (0.25 + Math.random() * 0.7)
      const fh = canvas.height * (0.03 + intensity * 0.3 + Math.random() * 0.12)
      const fx = Math.random() * Math.max(0, canvas.width - fw)
      const fy = pickY(baseY, fh)
      const fa =
        FLASH_ALPHA_MIN +
        intensity * (FLASH_ALPHA_MAX - FLASH_ALPHA_MIN) * Math.random()
      rects.push({
        x: fx,
        y: fy,
        w: fw,
        h: fh,
        rgb: NEON_WHITE,
        alpha: fa,
        createdAt: now,
        dieAt: now + FLASH_LIFETIME * (0.6 + Math.random() * 0.8),
        stroke: Math.random() < 0.45,
        isFlash: true,
        vy: 0,
      })
    }
  }

  // Advance stream position — large bursts push it further down the screen
  streamY = (streamY + bytes * STREAM_PER_BYTE) % 1

  // Prune oldest rects if over global cap
  if (rects.length > MAX_RECTS) rects.splice(0, rects.length - MAX_RECTS)
}

// ── RAF loop ──────────────────────────────────────────────────────────────
const tick = (now) => {
  const dt = lastTick > 0 ? (now - lastTick) / 1000 : 0 // seconds since last frame
  lastTick = now

  // Full clear each frame — essential for correct alpha compositing
  ctx.fillStyle = COLOR_BG
  ctx.fillRect(0, 0, canvas.width, canvas.height)

  // Draw rects oldest→newest so newer ones composite on top
  let i = 0
  while (i < rects.length) {
    const r = rects[i]

    // Move directional rects
    r.y += r.vy * dt

    // Death: off-screen for moving rects, lifetime for stationary/flash
    const offBottom = r.vy > 0 && r.y > canvas.height
    const offTop = r.vy < 0 && r.y + r.h < 0
    if (offBottom || offTop || now >= r.dieAt) {
      rects.splice(i, 1)
      continue
    }

    let alpha

    if (r.isFlash) {
      // Flash: cosine ease — hits instantly, fades smoothly
      const age = now - r.createdAt
      const lifetime = r.dieAt - r.createdAt
      alpha = r.alpha * Math.pow(Math.cos((age / lifetime) * Math.PI * 0.5), 2)
    } else if (r.vy !== 0) {
      // Moving rects: hold full alpha throughout travel
      alpha = r.alpha
    } else {
      // Stationary: hold alpha for first 15%, then linear fade to 0
      const age = now - r.createdAt
      const lifetime = r.dieAt - r.createdAt
      const hold = lifetime * 0.15
      const fadeT = age < hold ? 0 : (age - hold) / (lifetime - hold)
      alpha = r.alpha * Math.max(0, 1 - fadeT)
    }

    ctx.globalAlpha = alpha
    const [red, green, blue] = r.rgb
    const color = `rgb(${red},${green},${blue})`

    if (r.stroke) {
      ctx.strokeStyle = color
      // Slight line-width jitter per frame — deliberate glitch flicker
      ctx.lineWidth = 1 + Math.random() * 0.8
      ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w, r.h)
    } else {
      ctx.fillStyle = color
      ctx.fillRect(r.x, r.y, r.w, r.h)
    }

    i++
  }

  ctx.globalAlpha = 1
  requestAnimationFrame(tick)
}

requestAnimationFrame(tick)

// ── Canvas init / resize ──────────────────────────────────────────────────
const initCanvas = () => {
  rects.length = 0
  streamY = 0.05
  lastTick = 0
  canvas.width = window.innerWidth
  canvas.height = window.innerHeight
  ctx.fillStyle = COLOR_BG
  ctx.fillRect(0, 0, canvas.width, canvas.height)
}

window.addEventListener("resize", initCanvas)
initCanvas()

// ── WebSocket ─────────────────────────────────────────────────────────────
const connect = () => {
  const ws = new WebSocket(`ws://${location.host}`)

  ws.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data)
      if (data.type === "traffic" || data.type === "new_ip")
        spawnBurst(data.length || 0, data.direction)
    } catch {
      /* ignore */
    }
  }

  ws.onclose = () => setTimeout(connect, 3000)
  ws.onerror = () => ws.close()
}

connect()
