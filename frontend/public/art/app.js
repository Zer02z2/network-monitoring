// ── TUNABLE CONFIG ────────────────────────────────────────────────────────
const RECT_BASE_COUNT = 5 // minimum rects per burst — needs to be high enough to show cascade
const RECT_SCALE = 0.008 // additional rects per byte
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

const RECT_SPEED = 5000 // px/s — speed of the invisible sweep line across the screen

const FLASH_THRESHOLD = 600 // bytes — triggers white flash rect
const FLASH_ALPHA_MIN = 0.75
const FLASH_ALPHA_MAX = 0.95
const FLASH_LIFETIME = 50 // ms — white flash is short and brutal
const FLASH_MAX_COUNT = 3 // max white flash rects per burst

const MAX_QUEUE = 50 // max pending packets — new arrivals dropped if full
const DRAIN_PER_FRAME = 10 // max packets processed per RAF frame

const COLOR_BG = "#000000"
const NEON_RED = [255, 15, 45]
const NEON_BLUE = [2, 212, 240]
const NEON_WHITE = [255, 255, 255]
// ─────────────────────────────────────────────────────────────────────────

const canvas = document.getElementById("canvas")
const ctx = canvas.getContext("2d")

// All active rects: { x, y, w, h, rgb, alpha, spawnAt, createdAt, dieAt, stroke, isFlash }
const rects = []

// Packet queue — filled by WebSocket, drained by RAF tick
const packetQueue = [] // [{ bytes, direction }]

// ── Spawn burst ───────────────────────────────────────────────────────────
// direction: "incoming" — invisible line sweeps top→bottom; each rect spawns at the
//                         line's current Y, evenly timed across the full sweep duration.
//            "outgoing" — same but bottom→top.
//            undefined  — all rects spawn immediately at random Y (original behavior).
const spawnBurst = (bytes, direction) => {
  const count = Math.min(
    RECT_MAX_COUNT,
    RECT_BASE_COUNT + Math.floor(bytes * RECT_SCALE),
  )
  const now = performance.now()
  const maxW =
    RECT_MIN_W + Math.min(1, bytes / 2000) * (RECT_MAX_W - RECT_MIN_W)
  const maxH =
    RECT_MIN_H + Math.min(1, bytes / 3000) * (RECT_MAX_H - RECT_MIN_H)

  if (direction === "incoming" || direction === "outgoing") {
    // Total time for line to cross the full screen height at RECT_SPEED px/s
    const sweepMs = (canvas.height / RECT_SPEED) * 1000

    for (let i = 0; i < count; i++) {
      const t = count > 1 ? i / (count - 1) : 0 // 0..1 across sweep
      const spawnAt = now + t * sweepMs
      const lineY =
        direction === "incoming" ? t * canvas.height : (1 - t) * canvas.height
      const w = RECT_MIN_W + Math.random() * (maxW - RECT_MIN_W)
      const h = RECT_MIN_H + Math.random() * (maxH - RECT_MIN_H)
      const life =
        RECT_LIFETIME *
        (1 - RECT_LIFETIME_VAR * 0.5 + Math.random() * RECT_LIFETIME_VAR)
      rects.push({
        x: Math.random() * Math.max(0, canvas.width - w),
        y: Math.max(0, Math.min(canvas.height - h, lineY)),
        w,
        h,
        rgb: direction === "outgoing" ? NEON_RED : NEON_BLUE,
        alpha:
          RECT_ALPHA_MIN + Math.random() * (RECT_ALPHA_MAX - RECT_ALPHA_MIN),
        spawnAt,
        createdAt: spawnAt,
        dieAt: spawnAt + life,
        stroke: Math.random() < STROKE_CHANCE,
        isFlash: false,
      })
    }
  } else {
    // No direction: all spawn at once at fully random Y
    for (let i = 0; i < count; i++) {
      const w = RECT_MIN_W + Math.random() * (maxW - RECT_MIN_W)
      const h = RECT_MIN_H + Math.random() * (maxH - RECT_MIN_H)
      const life =
        RECT_LIFETIME *
        (1 - RECT_LIFETIME_VAR * 0.5 + Math.random() * RECT_LIFETIME_VAR)
      rects.push({
        x: Math.random() * Math.max(0, canvas.width - w),
        y: Math.random() * Math.max(0, canvas.height - h),
        w,
        h,
        rgb: direction === "outgoing" ? NEON_RED : NEON_BLUE,
        alpha:
          RECT_ALPHA_MIN + Math.random() * (RECT_ALPHA_MAX - RECT_ALPHA_MIN),
        spawnAt: now,
        createdAt: now,
        dieAt: now + life,
        stroke: Math.random() < STROKE_CHANCE,
        isFlash: false,
      })
    }
  }

  // White flash rects — always spawn immediately, random position
  if (bytes >= FLASH_THRESHOLD) {
    const intensity = Math.min(1, (bytes - FLASH_THRESHOLD) / 5000)
    const flashCount = 1 + Math.floor(intensity * (FLASH_MAX_COUNT - 1))
    for (let f = 0; f < flashCount; f++) {
      const fw = canvas.width * (0.25 + Math.random() * 0.7)
      const fh = canvas.height * (0.03 + intensity * 0.3 + Math.random() * 0.12)
      const fa =
        FLASH_ALPHA_MIN +
        intensity * (FLASH_ALPHA_MAX - FLASH_ALPHA_MIN) * Math.random()
      rects.push({
        x: Math.random() * Math.max(0, canvas.width - fw),
        y: Math.random() * Math.max(0, canvas.height - fh),
        w: fw,
        h: fh,
        rgb: NEON_WHITE,
        alpha: fa,
        spawnAt: now,
        createdAt: now,
        dieAt: now + FLASH_LIFETIME * (0.6 + Math.random() * 0.8),
        stroke: Math.random() < 0.45,
        isFlash: true,
      })
    }
  }
}

// ── RAF loop ──────────────────────────────────────────────────────────────
const tick = (now) => {
  // Drain packet queue — cap per frame to avoid stalls under heavy traffic
  for (let d = 0; d < DRAIN_PER_FRAME; d++) {
    if (packetQueue.length === 0) break
    const { bytes, direction } = packetQueue.shift()
    spawnBurst(bytes, direction)
  }

  ctx.fillStyle = COLOR_BG
  ctx.fillRect(0, 0, canvas.width, canvas.height)

  let i = 0
  while (i < rects.length) {
    const r = rects[i]

    // Not yet scheduled to appear — skip but keep in array
    if (now < r.spawnAt) {
      i++
      continue
    }

    // Lifetime expired — remove
    if (now >= r.dieAt) {
      rects.splice(i, 1)
      continue
    }

    const age = now - r.createdAt
    const lifetime = r.dieAt - r.createdAt
    let alpha

    if (r.isFlash) {
      // Cosine ease — hits instantly, fades smoothly
      alpha = r.alpha * Math.pow(Math.cos((age / lifetime) * Math.PI * 0.5), 2)
    } else {
      // Hold alpha for first 15%, then linear fade to 0
      const hold = lifetime * 0.15
      const fadeT = age < hold ? 0 : (age - hold) / (lifetime - hold)
      alpha = r.alpha * Math.max(0, 1 - fadeT)
    }

    ctx.globalAlpha = alpha
    const [red, green, blue] = r.rgb
    const color = `rgb(${red},${green},${blue})`

    if (r.stroke) {
      ctx.strokeStyle = color
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
  packetQueue.length = 0
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
      if (data.type === "traffic" || data.type === "new_ip") {
        if (packetQueue.length < MAX_QUEUE)
          packetQueue.push({
            bytes: data.length || 0,
            direction: data.direction,
          })
        // else drop silently — queue full, system is behind
      }
    } catch {
      /* ignore */
    }
  }

  ws.onclose = () => setTimeout(connect, 3000)
  ws.onerror = () => ws.close()
}

connect()
