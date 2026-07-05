"""Self-contained laptop-facing page for the boarding gate.

Served at GET / by src/main.py. No external assets (CSP-free, works offline
at the pier): all CSS/JS is inline. It lists upcoming trips from /trips,
downloads one via POST /sync, and polls /trip_status for live progress.
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Aleson Boarding Gate</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 16px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0;
  }
  header {
    padding: 20px 24px; background: #1e293b; border-bottom: 1px solid #334155;
  }
  header h1 { margin: 0; font-size: 20px; }
  header p { margin: 4px 0 0; color: #94a3b8; font-size: 14px; }
  main { max-width: 820px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .05em;
       color: #94a3b8; margin: 28px 0 12px; }
  .card {
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 16px 18px; margin-bottom: 12px;
  }
  .trip { display: flex; align-items: center; gap: 16px; }
  .trip .info { flex: 1; min-width: 0; }
  .trip .route { font-size: 18px; font-weight: 700; }
  .trip .meta { color: #94a3b8; font-size: 14px; margin-top: 2px; }
  button {
    font: inherit; font-weight: 600; cursor: pointer; border: 0;
    border-radius: 8px; padding: 10px 18px; background: #2563eb; color: #fff;
    white-space: nowrap;
  }
  button:hover { background: #1d4ed8; }
  button:disabled { opacity: .5; cursor: default; }
  .progress-wrap {
    background: #334155; border-radius: 999px; height: 14px; overflow: hidden;
    margin: 14px 0 6px;
  }
  .progress-bar {
    height: 100%; width: 0%; background: #16a34a; transition: width .4s ease;
  }
  .progress-num { font-size: 32px; font-weight: 800; }
  .progress-num small { font-size: 16px; font-weight: 600; color: #94a3b8; }
  .muted { color: #94a3b8; }
  #toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    background: #334155; color: #fff; padding: 12px 20px; border-radius: 10px;
    opacity: 0; pointer-events: none; transition: opacity .25s; max-width: 90vw;
  }
  #toast.show { opacity: 1; }
  #toast.err { background: #b91c1c; }
  #toast.ok { background: #15803d; }
</style>
</head>
<body>
<header>
  <h1>⛴ Aleson Boarding Gate</h1>
  <p>Download the upcoming trip you're boarding, then watch progress here.</p>
</header>
<main>
  <section id="loaded-section" style="display:none">
    <h2>Loaded trip — boarding progress</h2>
    <div class="card">
      <div id="loaded-route" class="trip"><div class="route">—</div></div>
      <div class="progress-wrap"><div id="progress-bar" class="progress-bar"></div></div>
      <div><span id="progress-num" class="progress-num">0<small> / 0 boarded</small></span></div>
    </div>
  </section>

  <section>
    <h2>Upcoming trips</h2>
    <div id="trips"><div class="card muted">Loading trips…</div></div>
  </section>
</main>
<div id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);

function toast(msg, kind) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show " + (kind || "");
  setTimeout(() => { t.className = t.className.replace("show", "").trim(); }, 3500);
}

function fmtDeparture(raw) {
  if (!raw) return "";
  const d = new Date(String(raw).replace(" ", "T"));
  if (isNaN(d)) return raw;
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

async function loadTrips() {
  const box = $("trips");
  try {
    const res = await fetch("/trips");
    const data = await res.json();
    if (data.status !== "success") throw new Error(data.reason || "failed");
    const trips = data.trips || [];
    if (!trips.length) {
      box.innerHTML = '<div class="card muted">No upcoming trips found.</div>';
      return;
    }
    box.innerHTML = "";
    for (const t of trips) {
      const route = (t.origin && t.destination)
        ? `${t.origin} → ${t.destination}` : "Unscheduled trip";
      const card = document.createElement("div");
      card.className = "card trip";
      card.innerHTML = `
        <div class="info">
          <div class="route"></div>
          <div class="meta"></div>
        </div>
        <button>Download</button>`;
      card.querySelector(".route").textContent = route;
      card.querySelector(".meta").textContent =
        `${t.vessel_name || "—"} · ${fmtDeparture(t.scheduled_departure)} · ${t.ticket_count} tickets`;
      const btn = card.querySelector("button");
      btn.onclick = () => downloadTrip(t.id, route, btn);
      box.appendChild(card);
    }
  } catch (e) {
    box.innerHTML = '<div class="card muted">Could not load trips (need internet to the cloud backend).</div>';
    toast("Failed to load trips: " + e.message, "err");
  }
}

async function downloadTrip(tripId, route, btn) {
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "Downloading…";
  try {
    const res = await fetch("/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trip_id: tripId }),
    });
    const data = await res.json();
    if (data.status !== "success") throw new Error(data.reason || "failed");
    toast(`Downloaded ${route}: ${data.tickets} tickets`, "ok");
    refreshStatus();
  } catch (e) {
    toast("Download failed: " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = original;
  }
}

async function refreshStatus() {
  try {
    const res = await fetch("/trip_status");
    const s = await res.json();
    if (s.status !== "ok" || !s.total) { $("loaded-section").style.display = "none"; return; }
    $("loaded-section").style.display = "";
    $("loaded-route").innerHTML =
      `<div class="info"><div class="route"></div><div class="meta"></div></div>`;
    $("loaded-route").querySelector(".route").textContent = s.route || "Loaded trip";
    $("loaded-route").querySelector(".meta").textContent =
      `${s.vessel || "—"} · ${fmtDeparture(s.departure)}`;
    const pct = s.total ? Math.round((s.boarded / s.total) * 100) : 0;
    $("progress-bar").style.width = pct + "%";
    $("progress-num").innerHTML = `${s.boarded}<small> / ${s.total} boarded (${pct}%)</small>`;
  } catch (e) { /* keep last known state */ }
}

loadTrips();
refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>"""
