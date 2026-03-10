const MAX = 500;

const tbody       = document.getElementById("tbody");
const dot         = document.getElementById("dot");
const statTotal   = document.getElementById("stat-total");
const statIps     = document.getElementById("stat-ips");
const statShown   = document.getElementById("stat-shown");
const ipTagsEl    = document.getElementById("ip-tags");
const ipNoneEl    = document.getElementById("ip-none");
const footerStatus = document.getElementById("footer-status");
const wrapper     = document.getElementById("table-wrapper");

let totalPackets = 0;
const knownIps = new Set();
let autoScroll = true;

// ── Scroll intent ─────────────────────────────────────────────────
wrapper.addEventListener("scroll", () => {
  autoScroll = wrapper.scrollHeight - wrapper.scrollTop - wrapper.clientHeight < 40;
});

// ── Helpers ───────────────────────────────────────────────────────
const esc = s =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const fmtTime = iso => {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", { hour12: false }) +
           "." + String(d.getMilliseconds()).padStart(3, "0");
  } catch { return iso; }
};

const protoClass = proto => {
  const p = (proto || "").toUpperCase();
  if (p === "TCP") return "proto-tcp";
  if (p === "UDP") return "proto-udp";
  if (p.includes("TLS") || p.includes("SSL")) return "proto-tls";
  if (p.includes("HTTP")) return "proto-http";
  return "proto-other";
};

const formatAddr = (ip, port) => {
  if (!ip) return "—";
  if (port) return `${esc(ip)}:<span style="color:var(--muted)">${port}</span>`;
  return esc(ip);
};

const addIpTag = (ip, source) => {
  if (knownIps.has(ip)) return;
  knownIps.add(ip);
  ipNoneEl.style.display = "none";
  const tag = document.createElement("span");
  tag.className = "ip-tag";
  const srcLabel = source === "dns" ? ` <span style="opacity:.6;font-size:10px">dns</span>`
                 : source === "sni" ? ` <span style="opacity:.6;font-size:10px">sni</span>`
                 : "";
  tag.innerHTML = esc(ip) + srcLabel;
  tag.title = source ? `Discovered via ${source}` : ip;
  ipTagsEl.appendChild(tag);
  statIps.textContent = knownIps.size;
};

// ── Triggered-by column ───────────────────────────────────────────
const fmtTriggered = d => {
  const names = d.matched_names;
  const nameTags = Array.isArray(names) && names.length
    ? names.map(n => `<span class="name-tag">${esc(n)}</span>`).join("")
    : "";

  let context = "";
  if (d.discovery_source === "dns" && d.dns_query) {
    context = `<span style="color:var(--orange);margin-left:4px">dns:${esc(d.dns_query)}</span>`;
  } else if (d.sni) {
    context = `<span style="color:var(--yellow);margin-left:4px">sni:${esc(d.sni)}</span>`;
  }

  return nameTags + context;
};

// ── Row builder ───────────────────────────────────────────────────
const buildRow = d => {
  if (d.type === "existing_ips") return null;

  const tr = document.createElement("tr");
  tr.className = d.type === "new_ip" ? "new-ip highlight" : "traffic";

  const typeBadge = d.type === "new_ip"
    ? `<span class="badge badge-new-ip">New IP</span>`
    : `<span class="badge badge-traffic">Traffic</span>`;

  const proto     = (d.protocol  || "").toUpperCase();
  const transport = (d.transport || "—");

  tr.innerHTML = `
    <td>${typeBadge}</td>
    <td style="color:var(--muted)">${fmtTime(d.timestamp)}</td>
    <td class="${protoClass(transport)}">${esc(transport)}</td>
    <td class="${protoClass(proto)}">${esc(proto)}</td>
    <td>${formatAddr(d.src_ip, d.src_port)}</td>
    <td>${formatAddr(d.dst_ip, d.dst_port)}</td>
    <td style="color:var(--muted);text-align:right">${d.length ?? "—"}</td>
    <td>${fmtTriggered(d)}</td>
  `;
  return tr;
};

// ── Insert row, enforce 500 cap ───────────────────────────────────
const insertRow = d => {
  if (d.type === "existing_ips") {
    (d.ips || []).forEach(ip => addIpTag(ip, null));
    return;
  }

  if (d.type === "new_ip" && d.discovered_ip) {
    addIpTag(d.discovered_ip, d.discovery_source);
  }

  totalPackets++;
  statTotal.textContent = totalPackets;

  const tr = buildRow(d);
  if (!tr) return;

  tbody.prepend(tr);

  while (tbody.rows.length > MAX) tbody.deleteRow(tbody.rows.length - 1);
  statShown.textContent = tbody.rows.length;

  if (autoScroll) wrapper.scrollTop = 0;
};

// ── WebSocket ─────────────────────────────────────────────────────
const connect = () => {
  const ws = new WebSocket(`ws://${location.host}`);

  ws.onopen = () => {
    dot.classList.add("connected");
    footerStatus.textContent = "Connected — receiving live traffic";
  };

  ws.onmessage = evt => {
    try { insertRow(JSON.parse(evt.data)); } catch { /* ignore parse errors */ }
  };

  ws.onclose = () => {
    dot.classList.remove("connected");
    footerStatus.textContent = "Disconnected — reconnecting in 3 s…";
    setTimeout(connect, 3000);
  };

  ws.onerror = err => {
    console.error("WS error", err);
    ws.close();
  };
};

connect();
