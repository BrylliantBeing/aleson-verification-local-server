"""Self-contained laptop-facing page for the boarding gate.

Served at GET / by src/main.py. No external assets (CSP-free, works offline
at the pier): all CSS/JS is inline. It lists upcoming trips from /trips,
downloads one via POST /sync, and polls /trip_status for live progress.

A gate staffer logs in (POST /login, checked against the locally synced
gate_staff roster) before the console appears. The session token doubles as
a short pairing code the operator types into the tablet app once, so scans
POSTed from the tablet to /verify carry the same identity.
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
  .login-wrap {
    max-width: 340px; margin: 60px auto 0; padding: 0 24px;
  }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; }
  .field input {
    width: 100%; font: inherit; padding: 10px 12px; border-radius: 8px;
    border: 1px solid #334155; background: #0f172a; color: #e2e8f0;
  }
  .field .with-toggle { position: relative; }
  .field .with-toggle input { padding-right: 68px; }
  .reveal-btn {
    position: absolute; right: 4px; top: 50%; transform: translateY(-50%);
    background: none; color: #94a3b8; font-size: 13px; font-weight: 600;
    padding: 6px 10px;
  }
  .reveal-btn:hover { background: #334155; color: #e2e8f0; }
  .login-wrap button { width: 100%; margin-top: 4px; }
  .login-error { color: #f87171; font-size: 14px; margin-top: 10px; min-height: 1em; }
  .pairing-code {
    font-size: 28px; font-weight: 800; letter-spacing: .1em; text-align: center;
    padding: 14px; background: #0f172a; border: 1px dashed #475569; border-radius: 10px;
    margin: 10px 0;
  }
  .staff-bar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 24px; background: #1e293b; border-bottom: 1px solid #334155;
    font-size: 14px; color: #94a3b8;
  }
  .staff-bar button { padding: 6px 14px; background: #334155; }
  .staff-bar button:hover { background: #475569; }
</style>
</head>
<body>
<div id="login-view" class="login-wrap">
  <h2 style="margin-top:40px">Gate staff login</h2>
  <div class="field">
    <label>Email</label>
    <input id="login-email" type="email" autocomplete="username" />
  </div>
  <div class="field">
    <label>Password</label>
    <div class="with-toggle">
      <input id="login-password" type="password" autocomplete="current-password" />
      <button id="reveal-btn" type="button" class="reveal-btn"
              aria-label="Show password" aria-pressed="false">Show</button>
    </div>
  </div>
  <button id="login-btn">Log in</button>
  <div id="login-error" class="login-error"></div>
  <p class="muted" style="font-size:13px">
    No account synced yet? Download a trip once first (sync also pulls the
    gate-staff roster), then log in.
  </p>
</div>

<div id="console-view" style="display:none">
<div class="staff-bar">
  <span id="staff-name"></span>
  <button id="logout-btn">Log out</button>
</div>
<header>
  <h1>⛴ Aleson Boarding Gate</h1>
  <p>Download the upcoming trip you're boarding, then watch progress here.</p>
</header>
<main>
  <section class="card" style="margin-bottom:20px">
    <h2 style="margin-top:0">Tablet pairing code</h2>
    <div id="pairing-code" class="pairing-code">—</div>
    <p class="muted" style="font-size:13px;margin:0">
      Enter this in the boarding-gate tablet app's settings so its scans are
      attributed to you. Valid for 12 hours.
    </p>
  </section>

  <section id="loaded-section" style="display:none">
    <h2>Loaded trip — boarding progress</h2>
    <div class="card">
      <div id="loaded-route" class="trip"><div class="route">—</div></div>
      <div class="progress-wrap"><div id="progress-bar" class="progress-bar"></div></div>
      <div><span id="progress-num" class="progress-num">0<small> / 0 boarded</small></span></div>
      <div class="trip" style="margin-top:14px">
        <button id="export-manifest-btn">Print Manifest &amp; Depart</button>
        <button id="force-sync-btn" style="background:#334155">Force Sync</button>
        <span id="outbox-status" class="muted" style="font-size:13px"></span>
      </div>
      <p class="muted" style="font-size:13px;margin:10px 0 0">
        Printing the manifest also marks this trip <strong>Departed</strong> in the
        admin dashboard. It is queued locally first, so it still works with no
        internet at the pier.
      </p>
    </div>
  </section>

  <section>
    <h2>Upcoming trips</h2>
    <div id="trips"><div class="card muted">Loading trips…</div></div>
  </section>
</main>
</div>
<div id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);

function getSession() {
  try { return JSON.parse(localStorage.getItem("gate_session") || "null"); }
  catch { return null; }
}

function authHeaders() {
  const session = getSession();
  return session ? { Authorization: "Bearer " + session.token } : {};
}

function showConsole(session) {
  $("login-view").style.display = "none";
  $("console-view").style.display = "";
  $("staff-name").textContent = session.name;
  $("pairing-code").textContent = session.token;
  loadTrips();
  refreshStatus();
  refreshOutboxStatus();
}

function showLogin() {
  localStorage.removeItem("gate_session");
  $("console-view").style.display = "none";
  $("login-view").style.display = "";
  // Never hand the next staffer a revealed field on a shared gate laptop.
  $("login-password").value = "";
  $("login-password").type = "password";
  $("reveal-btn").textContent = "Show";
  $("reveal-btn").setAttribute("aria-label", "Show password");
  $("reveal-btn").setAttribute("aria-pressed", "false");
}

async function doLogin() {
  const email = $("login-email").value.trim();
  const password = $("login-password").value;
  const errBox = $("login-error");
  errBox.textContent = "";
  $("login-btn").disabled = true;
  try {
    const res = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Login failed");
    localStorage.setItem("gate_session", JSON.stringify(data));
    showConsole(data);
  } catch (e) {
    errBox.textContent = e.message;
  } finally {
    $("login-btn").disabled = false;
  }
}

$("login-btn").onclick = doLogin;
$("login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
$("reveal-btn").onclick = () => {
  const field = $("login-password");
  const shown = field.type === "text";
  field.type = shown ? "password" : "text";
  const btn = $("reveal-btn");
  btn.textContent = shown ? "Show" : "Hide";
  btn.setAttribute("aria-label", shown ? "Show password" : "Hide password");
  btn.setAttribute("aria-pressed", shown ? "false" : "true");
  field.focus();
};
$("logout-btn").onclick = showLogin;
$("export-manifest-btn").onclick = exportManifest;
$("force-sync-btn").onclick = forceSyncBoarding;

const existingSession = getSession();
if (existingSession) { showConsole(existingSession); } else { showLogin(); }

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
      // Downloading a trip TRUNCATEs and rebuilds the local ticket table, so
      // the server requires a session for it on any laptop that has already
      // synced a roster. Only a first-time provisioning sync is unauthenticated,
      // because until one has run there is nobody to log in as.
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ trip_id: tripId }),
    });
    if (res.status === 401) {
      throw new Error("log in first to download a different sailing");
    }
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

async function refreshOutboxStatus() {
  try {
    const res = await fetch("/outbox_status");
    const s = await res.json();
    const box = $("outbox-status");
    if (s.status !== "ok") { box.textContent = ""; return; }
    const parts = [];
    if (s.pending > 0) {
      parts.push(`${s.pending} boarding scan${s.pending === 1 ? "" : "s"} waiting to sync`);
    }
    // A reported departure that has not reached the cloud yet means the trip is
    // still showing as Scheduled in the dashboard — worth saying out loud.
    if (s.departures_pending > 0) {
      parts.push(`${s.departures_pending} departure report${s.departures_pending === 1 ? "" : "s"} waiting to sync`);
    }
    box.textContent = parts.length ? parts.join(" · ") : "All boarding data synced";
  } catch (e) { /* keep last known state */ }
}

async function forceSyncBoarding() {
  const btn = $("force-sync-btn");
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "Syncing…";
  try {
    const res = await fetch("/push_boarding", { method: "POST" });
    const data = await res.json();
    if (data.status !== "ok") throw new Error(data.reason || "failed");
    toast(
      data.pushed > 0
        ? `Synced ${data.pushed} boarding scan${data.pushed === 1 ? "" : "s"}${data.pending ? `, ${data.pending} still pending` : ""}`
        : "Nothing to sync",
      "ok",
    );
  } catch (e) {
    toast("Force sync failed: " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = original;
    refreshOutboxStatus();
  }
}

// The number painted on the seat. A seat_map name must be unique across the
// whole vessel, but the painted numbers repeat between classes (M/V Antonia 2
// has an Economy 42 and an Aircon 42), so those seats are stored with a
// hyphenated class prefix: ECO-42, AC-42. Crew read the bare number.
function seatNumberLabel(seatNumber) {
  return String(seatNumber || "").replace(/^[A-Z]{2,4}-(?=[0-9])/, "");
}

function buildManifestHtml(data) {
  const rows = (data.passengers || []).map((p) => `
    <tr>
      <td>${seatNumberLabel(p.seat_number)}</td>
      <td>${p.passenger_name}</td>
      <td>${p.nationality || ""}</td>
      <td>${p.accommodation_class || ""}</td>
      <td>${p.boarding_status}</td>
      <td>${p.boarded_at ? fmtDeparture(p.boarded_at) : ""}</td>
    </tr>`).join("");
  return `<!doctype html>
<html><head><meta charset="utf-8" /><title>Boarding Manifest</title>
<style>
  body { font: 13px/1.4 system-ui, sans-serif; color: #0f172a; padding: 24px; }
  h1 { font-size: 18px; margin: 0 0 2px; }
  .meta { color: #475569; font-size: 13px; margin-bottom: 4px; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #cbd5e1; }
  th { background: #f1f5f9; }
  .summary { margin-top: 4px; font-weight: 600; }
</style></head>
<body>
  <h1>Boarding Manifest — ${data.route || "Unscheduled trip"}</h1>
  <div class="meta">${data.vessel || "—"} · ${fmtDeparture(data.departure)}</div>
  <div class="meta">Exported by ${data.exported_by} at ${fmtDeparture(new Date().toISOString())}</div>
  <div class="summary">${(data.passengers || []).length} passengers ·
    ${(data.passengers || []).filter((p) => p.boarding_status === "Boarded").length} boarded</div>
  <table>
    <thead><tr><th>Seat</th><th>Passenger</th><th>Nationality</th><th>Class</th><th>Status</th><th>Boarded At</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
</body></html>`;
}

function printManifestDocument(html) {
  const iframe = document.createElement("iframe");
  iframe.style.cssText = "position:fixed;width:0;height:0;border:0;visibility:hidden";
  document.body.appendChild(iframe);
  const doc = iframe.contentWindow.document;
  doc.open(); doc.write(html); doc.close();
  const cleanup = () => setTimeout(() => iframe.remove(), 1000);
  setTimeout(() => {
    iframe.contentWindow.focus();
    iframe.contentWindow.print();
    cleanup();
  }, 150);
}

// Printing the manifest is the last step before the vessel casts off, so it is
// what reports the trip as Departed to the cloud. Confirmed once, because it
// changes the trip's status in the admin dashboard.
async function exportManifest() {
  const btn = $("export-manifest-btn");
  const original = btn.textContent;
  const proceed = window.confirm(
    "Print the passenger manifest?\\n\\n" +
    "This also marks the loaded trip as DEPARTED in the admin dashboard. " +
    "Only do this once boarding is closed."
  );
  if (!proceed) return;
  btn.disabled = true; btn.textContent = "Preparing manifest…";
  try {
    const res = await fetch("/manifest", { headers: authHeaders() });
    if (res.status === 401) throw new Error("Log in again to export the manifest");
    const data = await res.json();
    if (data.status !== "ok") throw new Error(data.detail || "Failed to load manifest");
    printManifestDocument(buildManifestHtml(data));
    const report = data.departure_reported || {};
    if (report.reported) {
      toast("Manifest printed — trip marked Departed (queued for the cloud)", "ok");
    } else {
      toast(
        "Manifest printed, but departure was NOT reported: " +
        (report.reason || "unknown reason") + ". Set the status by hand.",
        "err"
      );
    }
    refreshOutboxStatus();
  } catch (e) {
    toast("Manifest export failed: " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = original;
  }
}

setInterval(refreshStatus, 3000);
setInterval(refreshOutboxStatus, 15000);
</script>
</body>
</html>"""
