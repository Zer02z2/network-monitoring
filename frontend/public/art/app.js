// ── TUNABLE CONFIG ────────────────────────────────────────────────────────
const GRID_SIZE         = 10   // px — width & height of each grid cell
const CELL_GAP          = 1    // px — gap between cells on each side
const BYTES_PER_GRID    = 20   // bytes per grid cell (ceil)
const DISAPPEAR_DELAY   = 100  // ms — pause before a packet's cells start fading
const DISAPPEAR_STEP    = 5    // ms — time between each cell erasing
const FLASH_THRESHOLD   = 300  // bytes — minimum size to trigger white flash bursts
const FLASH_BURST_BYTES = 100  // bytes per burst (decoupled from BYTES_PER_GRID)
const FLASH_SCALE_BYTES = 200  // bytes per interval — each interval adds 1 flash grid
const FLASH_ROUND_DELAY = 10   // ms — gap between bursts
const FLASH_DURATION    = 50   // ms — how long a grid stays white
const CYAN_CHANCE       = 0.15 // 0–1 — probability a new grid is cyan vs pink

const COLOR_BG    = "#000000"
const COLOR_PINK  = "#ff2d6f"
const COLOR_CYAN  = "#00ffe0"
const COLOR_WHITE = "#ffffff"
// ─────────────────────────────────────────────────────────────────────────

const canvas = document.getElementById("canvas")
const ctx    = canvas.getContext("2d")

let COLS = 0
let ROWS = 0

// ── Canvas primitives ─────────────────────────────────────────────────────
const drawCell = (col, row, color) => {
  ctx.fillStyle = color
  ctx.fillRect(
    col * GRID_SIZE + CELL_GAP,
    row * GRID_SIZE + CELL_GAP,
    GRID_SIZE - CELL_GAP * 2,
    GRID_SIZE - CELL_GAP * 2,
  )
}

const eraseCell = (col, row) => {
  ctx.fillStyle = COLOR_BG
  ctx.fillRect(col * GRID_SIZE, row * GRID_SIZE, GRID_SIZE, GRID_SIZE)
}

// ── Grid ──────────────────────────────────────────────────────────────────
// Each grid tracks a _flashUntil timestamp instead of a timer handle.
// The RAF loop restores color when the timestamp is reached.
class Grid {
  constructor(col, relRow, color, packet) {
    this.col         = col
    this.relRow      = relRow
    this.color       = color   // mutable — swapOneColor changes this live
    this.packet      = packet  // back-ref so absRow is always current
    this.cleared     = false
    this._flashUntil = 0       // RAF timestamp for flash restore; 0 = not flashing
  }

  get absRow() { return this.packet.rowStart + this.relRow }

  draw() {
    if (!this.cleared) drawCell(this.col, this.absRow, this.color)
  }

  // Locked: ignored if already flashing (_flashUntil > 0)
  flash() {
    if (this.cleared || this._flashUntil > 0) return
    this._flashUntil = performance.now() + FLASH_DURATION
    drawCell(this.col, this.absRow, COLOR_WHITE)
    this.packet.swapOneColor()
  }

  // Fade wins: clears flash state immediately without waiting
  erase() {
    this._flashUntil = 0
    this.cleared     = true
    eraseCell(this.col, this.absRow)
  }

  stopFlash() { this._flashUntil = 0 }
}

// ── Packet ────────────────────────────────────────────────────────────────
// Uses RAF timestamps instead of setInterval/setTimeout.
// tick(now) is called every frame; returns true when fully faded.
class Packet {
  constructor(bytes, rowStart) {
    this.rowStart = rowStart

    const gridCount   = Math.max(1, Math.ceil(bytes / BYTES_PER_GRID))
    this.rowCount     = Math.ceil(gridCount / COLS)

    this.grids = Array.from({ length: gridCount }, (_, i) =>
      new Grid(
        i % COLS,
        Math.floor(i / COLS),
        Math.random() < CYAN_CHANCE ? COLOR_CYAN : COLOR_PINK,
        this,
      )
    )

    this._fadeStartAt = Infinity
    this._nextEraseAt = Infinity
    this._fadeIdx     = gridCount - 1
    this._done        = false
  }

  draw() { this.grids.forEach(g => g.draw()) }

  start() {
    this.draw()
    const now         = performance.now()
    this._fadeStartAt = now + DISAPPEAR_DELAY
    this._nextEraseAt = this._fadeStartAt
  }

  // Called every RAF frame. Returns true when this packet is fully gone.
  tick(now) {
    if (this._done) return false

    // Restore grids whose flash duration has elapsed
    for (const g of this.grids) {
      if (!g.cleared && g._flashUntil > 0 && now >= g._flashUntil) {
        g._flashUntil = 0
        drawCell(g.col, g.absRow, g.color)
      }
    }

    // Fade: erase one grid per DISAPPEAR_STEP after DISAPPEAR_DELAY
    if (now < this._fadeStartAt || now < this._nextEraseAt) return false

    while (this._fadeIdx >= 0 && this.grids[this._fadeIdx].cleared) this._fadeIdx--
    if (this._fadeIdx < 0) {
      this._done = true
      return true  // signal PacketManager to call _onGone
    }

    this.grids[this._fadeIdx--].erase()
    this._nextEraseAt = now + DISAPPEAR_STEP
    return false
  }

  // Stop all animation for this packet (resize / eviction)
  stopTimers() {
    this._fadeStartAt = Infinity
    this._nextEraseAt = Infinity
    this._fadeIdx     = -1
    this._done        = true
    this.grids.forEach(g => g.stopFlash())
  }

  // Swap one random cyan ↔ one random pink within this packet.
  swapOneColor() {
    const live  = this.grids.filter(g => !g.cleared)
    const cyans = live.filter(g => g.color === COLOR_CYAN)
    const pinks = live.filter(g => g.color === COLOR_PINK)
    if (!cyans.length || !pinks.length) return

    const cyanGrid = cyans[Math.floor(Math.random() * cyans.length)]
    const pinkGrid = pinks[Math.floor(Math.random() * pinks.length)]

    cyanGrid.color = COLOR_PINK
    pinkGrid.color = COLOR_CYAN

    if (!cyanGrid._flashUntil) cyanGrid.draw()
    if (!pinkGrid._flashUntil) pinkGrid.draw()
  }

  // Push burst entries into the global flash queue instead of using setTimeout
  triggerFlashBursts(burstCount, getAllLitGrids, flashGrids) {
    const now = performance.now()
    for (let b = 0; b < burstCount; b++) {
      flashQueue.push({ at: now + b * FLASH_ROUND_DELAY, getAllLitGrids, flashGrids })
    }
  }
}

// ── Flash queue — populated by triggerFlashBursts, drained by RAF tick ────
const flashQueue = []

// ── PacketManager ─────────────────────────────────────────────────────────
class PacketManager {
  constructor() {
    this.packets      = []
    this.nextRowStart = 0
  }

  add(bytes) {
    // Evict top packets until the new packet fits within the canvas
    const newRowCount = Math.ceil(Math.max(1, Math.ceil(bytes / BYTES_PER_GRID)) / COLS)
    while (this.packets.length && this.nextRowStart + newRowCount > ROWS) {
      this._evictTop()
    }

    const p = new Packet(bytes, this.nextRowStart)
    this.packets.push(p)
    this.nextRowStart += p.rowCount
    p.start()

    if (bytes > FLASH_THRESHOLD) {
      const burstCount = Math.floor((bytes - FLASH_THRESHOLD) / FLASH_BURST_BYTES)
      const flashGrids = Math.max(1, Math.floor(bytes / FLASH_SCALE_BYTES))
      p.triggerFlashBursts(burstCount, () => this._allLitGrids(), flashGrids)
    }
  }

  _allLitGrids() {
    return this.packets.flatMap(p => p.grids.filter(g => !g.cleared))
  }

  // Force-remove the oldest (top) packet; blit remaining pixels upward.
  // Flashes all surviving lit grids once as a visual eviction signal.
  _evictTop() {
    const top = this.packets.shift()
    top.stopTimers()

    const freed     = top.rowCount
    for (const p of this.packets) p.rowStart -= freed
    this.nextRowStart -= freed

    const srcY      = freed * GRID_SIZE
    const remaining = canvas.height - srcY
    if (remaining > 0) {
      ctx.drawImage(canvas, 0, srcY, canvas.width, remaining, 0, 0, canvas.width, remaining)
    }
    ctx.fillStyle = COLOR_BG
    ctx.fillRect(0, canvas.height - freed * GRID_SIZE, canvas.width, freed * GRID_SIZE)

    this._allLitGrids().forEach(g => g.flash())
  }

  _onGone(packet) {
    const idx = this.packets.indexOf(packet)
    if (idx === -1) return
    this.packets.splice(idx, 1)

    const freed = packet.rowCount
    for (let i = idx; i < this.packets.length; i++) {
      this.packets[i].rowStart -= freed
    }
    this.nextRowStart -= freed

    ctx.fillStyle = COLOR_BG
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    this.packets.forEach(p => p.draw())
  }

  clear() {
    this.packets.forEach(p => p.stopTimers())
    this.packets.length = 0
    this.nextRowStart   = 0
    flashQueue.length   = 0
  }
}

const manager = new PacketManager()

// ── RAF loop ──────────────────────────────────────────────────────────────
const tick = (now) => {
  // Fire due flash bursts (iterate backwards so splice indices stay valid)
  for (let i = flashQueue.length - 1; i >= 0; i--) {
    if (now < flashQueue[i].at) continue
    const { getAllLitGrids, flashGrids } = flashQueue.splice(i, 1)[0]
    const lit   = getAllLitGrids()
    const count = Math.min(flashGrids, lit.length)
    for (let j = 0; j < count; j++) {
      const k = j + Math.floor(Math.random() * (lit.length - j))
      ;[lit[j], lit[k]] = [lit[k], lit[j]]
      lit[j].flash()
    }
  }

  // Tick each packet; collect finished ones to avoid mutating the array mid-loop
  const done = []
  for (const p of manager.packets) {
    if (p.tick(now)) done.push(p)
  }
  for (const p of done) manager._onGone(p)

  requestAnimationFrame(tick)
}

requestAnimationFrame(tick)

// ── Canvas init / resize ──────────────────────────────────────────────────
const initCanvas = () => {
  manager.clear()
  canvas.width  = window.innerWidth
  canvas.height = window.innerHeight
  COLS = Math.floor(canvas.width  / GRID_SIZE)
  ROWS = Math.floor(canvas.height / GRID_SIZE)
  ctx.fillStyle = COLOR_BG
  ctx.fillRect(0, 0, canvas.width, canvas.height)
}

window.addEventListener("resize", initCanvas)
initCanvas()

// ── WebSocket ─────────────────────────────────────────────────────────────
const connect = () => {
  const ws = new WebSocket(`ws://${location.host}`)

  ws.onmessage = evt => {
    try {
      const data = JSON.parse(evt.data)
      if (data.type === "traffic" || data.type === "new_ip") manager.add(data.length || 0)
    } catch { /* ignore parse errors */ }
  }

  ws.onclose = () => setTimeout(connect, 3000)
  ws.onerror = () => ws.close()
}

connect()
