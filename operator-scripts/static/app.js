const map = L.map("map").setView([56.699893, 13.002148], 16);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20,
}).addTo(map);

const markers = {};
const missionFleet = new Set();
let selectedUsvId = 2;
let lastFleet = [];

let latestEnvironment = null;

// Fixed wind widget in top-right corner
const windControl = L.control({ position: "topright" });

windControl.onAdd = function () {
  const div = L.DomUtil.create("div", "leaflet-bar wind-control");
  div.style.background = "white";
  div.style.color = "black";
  div.style.padding = "8px";
  div.style.borderRadius = "8px";
  div.style.boxShadow = "0 1px 6px rgba(0,0,0,0.35)";
  div.style.textAlign = "center";
  div.style.minWidth = "78px";
  div.style.fontFamily = "Arial";
  div.style.pointerEvents = "none";
  return div;
};

windControl.addTo(map);

function updateWindControl(env) {
  const container = windControl.getContainer();

  if (
    env.wind_speed === null || env.wind_speed === undefined ||
    env.wind_direction === null || env.wind_direction === undefined
  ) {
    container.innerHTML = `
      <div style="font-weight:bold;font-size:14px;">WIND</div>
      <div style="font-size:12px;">No data</div>
    `;
    return;
  }

  // Meteorological wind direction = direction wind comes FROM.
  // Arrow shows where wind is blowing TO.
  const arrowDirection = (Number(env.wind_direction) + 180) % 360;
  const windSpeed = Number(env.wind_speed).toFixed(1);

  container.innerHTML = `
    <div style="font-weight:bold;font-size:14px;margin-bottom:4px;">WIND</div>

    <div style="
      font-size:42px;
      line-height:1;
      color:#0057ff;
      font-weight:bold;
      transform:rotate(${arrowDirection}deg);
      transform-origin:center center;
      text-shadow:
        -1px -1px 0 white,
         1px -1px 0 white,
        -1px  1px 0 white,
         1px  1px 0 white,
         0 0 4px rgba(0,0,0,0.5);
    ">➜</div>

    <div style="
      font-size:16px;
      font-weight:bold;
      margin-top:5px;
      white-space:nowrap;
    ">
      ${windSpeed} m/s
    </div>

    <div style="
      font-size:11px;
      color:#555;
      margin-top:2px;
      white-space:nowrap;
    ">
      from ${env.wind_direction}°
    </div>
  `;
}

function makeUsvIcon(usv) {
  const color = usv.online ? "#00ff55" : "#ff3333";
  const heading = usv.heading ?? 0;

  return L.divIcon({
    className: "",
    html: `
      <div style="width:40px;height:40px;text-align:center;">
        <div style="
          color:white;
          font-size:22px;
          line-height:18px;
          transform:rotate(${heading}deg);
          transform-origin:center;
          text-shadow:-1px -1px 0 black,1px -1px 0 black,-1px 1px 0 black,1px 1px 0 black;
        ">➜</div>

        <div style="
          color:${color};
          font-size:15px;
          font-weight:bold;
          text-shadow:-1px -1px 0 black,1px -1px 0 black,-1px 1px 0 black,1px 1px 0 black,0 0 4px black;
        ">${usv.id}</div>
      </div>
    `,
    iconSize: [40, 40],
    iconAnchor: [20, 20],
  });
}

function addToMission(id) {
  missionFleet.add(id);
  updateFleet();
}

function removeFromMission(id) {
  missionFleet.delete(id);
  updateFleet();
}

function selectUsv(id) {
  selectedUsvId = id;
  renderDetails();
}

function valueOrUnknown(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "?";
  return `${value}${suffix}`;
}

function detailValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "?";
  if (Array.isArray(value) || typeof value === "object") {
    return `${JSON.stringify(value)}${suffix}`;
  }
  return `${value}${suffix}`;
}

function commClass(commState) {
  const s = String(commState || "unknown").toLowerCase();
  if (s === "connected") return "status-connected";
  if (s === "partitioned") return "status-partitioned";
  if (s === "disconnected") return "status-disconnected";
  return "status-unknown";
}

function usvCard(usv, inMissionFleet) {
  const dot = usv.online ? "🟢" : "🔴";
  const battery = valueOrUnknown(usv.battery, "%");
  const speed = valueOrUnknown(usv.speed, " m/s");
  const coverage = valueOrUnknown(usv.coverage, "%");
  const commState = usv.comm_state ?? usv.comms ?? "UNKNOWN";

  const button = inMissionFleet
    ? `<button onclick="removeFromMission(${usv.id})">Remove</button>`
    : `<button onclick="addToMission(${usv.id})">Add</button>`;

  return `
    <div class="usv-card" onclick="selectUsv(${usv.id})">
      <div><b>USV ${usv.id}</b> ${dot} ${valueOrUnknown(usv.status)}</div>
      <div>Battery: ${battery}</div>
      <div>Comms: <span class="${commClass(commState)}">${commState}</span></div>
      <div>Age: ${valueOrUnknown(usv.last_seen_age_s, " s")}</div>
      <div>Speed: ${speed}</div>
      <div>Mission: ${valueOrUnknown(usv.mission)}</div>
      <div>Coverage: ${coverage}</div>
      ${button}
    </div>
  `;
}

function renderDetails() {
  const details = document.getElementById("details");
  const usv = lastFleet.find(u => u.id === selectedUsvId);

  if (!usv) {
    details.innerHTML = "No live data";
    return;
  }

  const telemetry = usv.telemetry || {};
  const agentState = usv.agent_state || usv.agent || {};
  const mission = usv.mission_data || {};
  const communication = usv.communication || {};
  const health = usv.health || {};
  const fleetInfo = usv.fleet_info || {};
  const measurements = usv.measurements || {};
  const events = usv.events || [];
  const commState = usv.comm_state ?? usv.comms ?? "UNKNOWN";

  details.innerHTML = `
    <div class="kv"><span class="label">Name:</span> ${valueOrUnknown(usv.name)}</div>
    <div class="kv"><span class="label">ID:</span> ${valueOrUnknown(usv.id)}</div>
    <div class="kv"><span class="label">Online:</span> ${usv.online ? "Yes" : "No"}</div>
    <div class="kv"><span class="label">Communication:</span> <span class="${commClass(commState)}">${commState}</span></div>
    <div class="kv"><span class="label">Last seen:</span> ${valueOrUnknown(usv.last_seen)}</div>
    <div class="kv"><span class="label">Last seen age:</span> ${valueOrUnknown(usv.last_seen_age_s, " s")}</div>

    <h4>Telemetry</h4>
    <div class="kv"><span class="label">Latitude:</span> ${valueOrUnknown(usv.lat)}</div>
    <div class="kv"><span class="label">Longitude:</span> ${valueOrUnknown(usv.lng)}</div>
    <div class="kv"><span class="label">Altitude:</span> ${valueOrUnknown(telemetry.alt, " m")}</div>
    <div class="kv"><span class="label">Heading:</span> ${valueOrUnknown(usv.heading, "°")}</div>
    <div class="kv"><span class="label">Ground speed:</span> ${valueOrUnknown(usv.speed, " m/s")}</div>
    <div class="kv"><span class="label">Battery:</span> ${valueOrUnknown(usv.battery, "%")}</div>
    <div class="kv"><span class="label">Armed:</span> ${valueOrUnknown(telemetry.armed)}</div>
    <div class="kv"><span class="label">Mode:</span> ${valueOrUnknown(telemetry.mode)}</div>

    <h4>Mission</h4><div class="kv"><span class="label">State:</span> ${valueOrUnknown(mission.mission_state)}</div><div class="kv"><span class="label">Active:</span> ${valueOrUnknown(mission.mission_active)}</div><div class="kv"><span class="label">Waypoint:</span> ${valueOrUnknown(mission.current_waypoint_display)}</div><div class="kv"><span class="label">Mission count:</span> ${valueOrUnknown(mission.mission_count)}</div>

    <h4>Communication Details</h4><div class="kv"><span class="label">Connectivity:</span> ${valueOrUnknown(communication.connectivity)}</div><div class="kv"><span class="label">Operator reachable:</span> ${valueOrUnknown(communication.operator_reachable)}</div><div class="kv"><span class="label">Buffered packets:</span> ${valueOrUnknown(communication.buffered_packets)}</div>

    <h4>Health</h4><div class="kv"><span class="label">CPU load:</span> ${valueOrUnknown(health.cpu_load)}</div><div class="kv"><span class="label">Disk:</span> ${valueOrUnknown(health.disk_usage, "%")}</div><div class="kv"><span class="label">Flask:</span> ${valueOrUnknown(health.flask_status)}</div><div class="kv"><span class="label">Leak:</span> ${valueOrUnknown(health.leak_detected)}</div>

    <h4>Measurements</h4>
    <div class="kv"><span class="label">Water quality:</span> ${detailValue(measurements.water_quality)}</div>
    <div class="kv"><span class="label">Bathymetry:</span> ${detailValue(measurements.bathymetry)}</div>

    <h4>Fleet</h4>
    <div class="kv"><span class="label">Role:</span> ${detailValue(fleetInfo.fleet_role)}</div>
    <div class="kv"><span class="label">Sector:</span> ${detailValue(fleetInfo.assigned_sector)}</div>
    <div class="kv"><span class="label">Formation:</span> ${detailValue(fleetInfo.formation)}</div>

    <h4>Events</h4>
    <div class="kv"><span class="label">Count:</span> ${Array.isArray(events) ? events.length : "?"}</div>

    <h4>Agent</h4>
    <div class="kv"><span class="label">Message type:</span> ${valueOrUnknown(agentState.message_type)}</div>
    <div class="kv"><span class="label">Schema:</span> ${valueOrUnknown(agentState.schema_version)}</div>
    <div class="kv"><span class="label">Source:</span> ${valueOrUnknown(agentState.source)}</div>
    <div class="kv"><span class="label">Target:</span> ${valueOrUnknown(agentState.target)}</div>
    <div class="kv"><span class="label">Groups:</span> ${Array.isArray(agentState.groups) ? agentState.groups.join(", ") : "?"}</div>

    <h4>Raw message</h4>
    <details>
      <summary>Raw message</summary>
      <pre>${JSON.stringify(usv.raw || {}, null, 2)}</pre>
    </details>
  `;
}

async function updateFleet() {
  const res = await fetch("/api/fleet/status");
  const fleet = await res.json();
  lastFleet = fleet;

  const fleetList = document.getElementById("fleet-list");
  const missionList = document.getElementById("mission-list");

  fleetList.innerHTML = "";
  missionList.innerHTML = "";

  fleet.forEach(usv => {
    const inMissionFleet = missionFleet.has(usv.id);

    if (inMissionFleet) {
      missionList.innerHTML += usvCard(usv, true);
    } else {
      fleetList.innerHTML += usvCard(usv, false);
    }

    const latlng = [usv.lat, usv.lng];

    if (!markers[usv.id]) {
      markers[usv.id] = L.marker(latlng, {
        icon: makeUsvIcon(usv),
      }).addTo(map);

      markers[usv.id].on("click", () => selectUsv(usv.id));
    } else {
      markers[usv.id].setLatLng(latlng);
      markers[usv.id].setIcon(makeUsvIcon(usv));
    }

    markers[usv.id].bindPopup(`
      <b>${usv.name}</b><br>
      ID: ${usv.id}<br>
      Status: ${valueOrUnknown(usv.status)}<br>
      Battery: ${valueOrUnknown(usv.battery, "%")}<br>
      Comms: ${valueOrUnknown(usv.comm_state ?? usv.comms)}<br>
      Heading: ${valueOrUnknown(usv.heading, "°")}<br>
      Speed: ${valueOrUnknown(usv.speed, " m/s")}<br>
      Mission: ${valueOrUnknown(usv.mission)}
    `);
  });

  if (missionFleet.size === 0) {
    missionList.innerHTML = "No active USVs";
  }

  renderDetails();
}

async function updateEnvironment() {
  const environmentDiv = document.getElementById("environment");

  try {
    const res = await fetch("/api/environment");
    const env = await res.json();

    latestEnvironment = env;
    updateWindControl(env);

    environmentDiv.innerHTML = `
      <div class="kv"><span class="label">Local time:</span> ${valueOrUnknown(env.local_time)}</div>
      <div class="kv"><span class="label">Temperature:</span> ${valueOrUnknown(env.temperature, " °C")}</div>
      <div class="kv"><span class="label">Wind speed:</span> ${valueOrUnknown(env.wind_speed, " m/s")}</div>
      <div class="kv"><span class="label">Wind direction:</span> ${valueOrUnknown(env.wind_direction, "°")}</div>
    `;
  } catch (e) {
    environmentDiv.innerHTML = "Environment unavailable";
    updateWindControl({});
  }
}

setInterval(updateFleet, 2000);
setInterval(updateEnvironment, 10000);

updateFleet();
updateEnvironment();