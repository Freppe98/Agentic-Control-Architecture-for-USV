const map = L.map("map").setView([56.699893, 13.002148], 16);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20,
}).addTo(map);

const markers = {};
const missionFleet = new Set();

function makeUsvIcon(usv) {
  const color = usv.online ? "#00ff55" : "#ff3333";

  return L.divIcon({
    className: "",
    html: `
      <div style="width:40px;height:40px;text-align:center;">
        <div style="
          color:white;
          font-size:22px;
          line-height:18px;
          transform:rotate(${usv.heading}deg);
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

function usvCard(usv, inMissionFleet) {
  const dot = usv.online ? "🟢" : "🔴";
  const battery = usv.battery !== null ? `${usv.battery}%` : "?";
  const speed = usv.speed !== null ? `${usv.speed} m/s` : "?";
  const coverage = usv.coverage !== null ? `${usv.coverage}%` : "?";

  const button = inMissionFleet
    ? `<button onclick="removeFromMission(${usv.id})">Remove</button>`
    : `<button onclick="addToMission(${usv.id})">Add</button>`;

  return `
    <div class="usv-card">
      <div><b>USV ${usv.id}</b> ${dot} ${usv.status}</div>
      <div>Battery: ${battery}</div>
      <div>Comms: ${usv.comms}</div>
      <div>Speed: ${speed}</div>
      <div>Mission: ${usv.mission}</div>
      <div>Coverage: ${coverage}</div>
      ${button}
    </div>
  `;
}

async function updateFleet() {
  const res = await fetch("/api/fleet/status");
  const fleet = await res.json();

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
    } else {
      markers[usv.id].setLatLng(latlng);
      markers[usv.id].setIcon(makeUsvIcon(usv));
    }

    markers[usv.id].bindPopup(`
      <b>${usv.name}</b><br>
      ID: ${usv.id}<br>
      Status: ${usv.status}<br>
      Battery: ${batteryValue(usv)}<br>
      Comms: ${usv.comms}<br>
      Heading: ${usv.heading}°<br>
      Speed: ${speedValue(usv)}<br>
      Mission: ${usv.mission}<br>
      Coverage: ${coverageValue(usv)}
    `);
  });

  if (missionFleet.size === 0) {
    missionList.innerHTML = "No active USVs";
  }
}

function batteryValue(usv) {
  return usv.battery !== null ? `${usv.battery}%` : "?";
}

function speedValue(usv) {
  return usv.speed !== null ? `${usv.speed} m/s` : "?";
}

function coverageValue(usv) {
  return usv.coverage !== null ? `${usv.coverage}%` : "?";
}

setInterval(updateFleet, 2000);
updateFleet();