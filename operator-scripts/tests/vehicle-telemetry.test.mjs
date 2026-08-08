// Regression tests for the Vehicle page's diagnostic-row derivations
// (operator/lib/vehicle-telemetry.js).
//
// Every case here is a row that was WRONG on the bench run: a permanent "— NO TELEM"
// placeholder over data Scout was sending every second, a structured value coerced into
// "[object Object]", an uncalibrated sensor one step from reading "safe", or a null that
// would have been rendered identically to a real zero.
//
// The page's SOURCE is checked too: a row that is derived correctly here can still be
// re-broken by someone hardcoding naRow() back over it, so the section markup is asserted
// against the derivations rather than only the derivations against themselves.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  ST, num, tri, present, humanizeToken,
  ekfRow, mavlinkRow, homeVerificationRow, currentWaypointRow, missionLoadedRow,
  missionReadbackRow, batteryVoltageRow, batteryCurrentRow, powerSourceRow,
  batteryRemainingRow, failsafeRow, wireguardRow, rttRow, packetLossRow,
  operatorConnectedRow, agentRows, gpsSatellitesRow, imuRow, leakSensorRow,
  bathymetryRow, cameraRow, temperatureRow, serviceStatusRow,
} from "../operator/lib/vehicle-telemetry.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const vehicleSrc = readFileSync(join(HERE, "..", "operator", "pages", "Vehicle.js"), "utf8");
// Source assertions must look at CODE, not at the comments explaining the bug that was
// fixed — otherwise the comment describing `String(a.current_policy)` fails the test
// forbidding `String(a.current_policy)`.
const vehicleCode = vehicleSrc
  .replace(/\/\*[\s\S]*?\*\//g, "")
  // `[^\n]` rather than `.*$` — JS's `.` does not match the CR of a CRLF line ending,
  // so an anchored `.*$` silently strips nothing on a Windows checkout.
  .split("\n").map((l) => l.replace(/(^|[^:])\/\/[^\n]*/, "$1")).join("\n");
const LIVE = JSON.parse(readFileSync(join(HERE, "fixtures", "scout-status-live.json"), "utf8"));

/** A fleet row shaped the way the backend now builds it, from the live packet. */
function liveRow(over = {}) {
  const p = LIVE.payload;
  return {
    id: 2, name: "Scout", comm_state: "CONNECTED", online: true,
    lat: p.telemetry.lat, lng: p.telemetry.lng, heading: p.telemetry.heading,
    battery: p.power.battery_remaining_pct,
    telemetry: p.telemetry,
    health: p.health,
    power: {
      battery_voltage_v: p.power.battery_voltage_v,
      battery_current_a: p.power.battery_current_a,
      battery_remaining_pct: p.power.battery_remaining_pct,
      source: p.power.source,
    },
    failsafe: { status: p.failsafe.status, system_state: p.failsafe.system_state,
                unhealthy_sensors_present: p.failsafe.unhealthy_sensors_present },
    imu: { available: true, health: p.imu.imu_health, last_seen_s: p.imu.imu_last_seen_s },
    mavlink: { connected: p.mavlink.mavlink_connected,
               heartbeat_age_s: p.mavlink.heartbeat_age_s,
               msg_rate_hz: p.mavlink.mavlink_msg_rate_hz },
    mission_status: {
      mission_count: p.mission.mission_count,
      current_waypoint: p.mission.current_waypoint,
      current_waypoint_display: p.mission.current_waypoint_display,
      readback_available: true, readback: { age_s: 2.5 },
    },
    home: { verified: p.agent.home_status.verified, reason: p.agent.home_status.reason,
            available: true, lat: 56.65, lng: 12.87 },
    agent_summary: {
      current_behaviour: "monitoring", current_decision: "Hold Position",
      current_policy: "FULL_REPORTING", mission_policy: "SUPERVISED_CONTINUATION",
      decision_reason: "No mission assigned; standing by.", autonomy_level: "ASSISTED",
    },
    leak_sensor: { state: "UNCALIBRATED", available: true, signal: "LOW",
                   polarity: "UNCALIBRATED", leak_detected: null },
    sampling: { enabled: false, reported: true, has_reading: false },
    service_status: {
      reported: true,
      services: { local_mission_agent: "ONLINE", vehicle_api: "ONLINE",
                  pixhawk_link: "ONLINE", sensor_service: "ONLINE",
                  gpio_service: "ONLINE", influx: "UNKNOWN" },
      online: ["gpio_service", "local_mission_agent", "pixhawk_link", "sensor_service",
               "vehicle_api"],
      offline: [], unknown: ["influx"], required_offline: [], total: 6,
    },
    link: {
      operator_connected: p.communication.operator_connected,
      operator_reachable: p.communication.operator_reachable,
      rtt_ms: p.communication.rtt_ms, seq: p.communication.seq,
      vpn: p.communication.vpn_status,
      packet_loss: { state: "UNMEASURED", loss_pct: null, samples: 4, min_samples: 20,
                     window_s: 120 },
    },
    ...over,
  };
}

// ════════════════════════════════════════════════════════════════════════════════════
// Primitives — the "0 is not absence" rule the whole page depends on
// ════════════════════════════════════════════════════════════════════════════════════

test("num() keeps a real zero and rejects bools/null", () => {
  assert.equal(num(0), 0);
  assert.equal(num(0.0), 0);
  assert.equal(num(null), null);
  assert.equal(num(undefined), null);
  assert.equal(num(true), null, "true must never read as 1");
  assert.equal(num("5"), null, "a string is not a measurement");
});

test("tri() distinguishes false from not-observed", () => {
  assert.equal(tri(false), false);
  assert.equal(tri(true), true);
  assert.equal(tri(null), null);
  assert.equal(tri(0), null, "0 is not a boolean false");
});

test("present() is the explicit replacement for a truthiness test", () => {
  assert.equal(present(0), true);
  assert.equal(present(""), true);
  assert.equal(present(null), false);
  assert.equal(present(undefined), false);
});

test("humanizeToken turns a shouted token into prose but leaves sentences alone", () => {
  assert.equal(humanizeToken("PIXHAWK_BATTERY_MONITOR"), "Pixhawk battery monitor");
  assert.equal(humanizeToken("RECENT_HANDSHAKE"), "Recent handshake");
  assert.equal(humanizeToken("FULL_REPORTING"), "Full reporting");
  assert.equal(humanizeToken("Hold Position"), "Hold Position");
  assert.equal(humanizeToken("Home has not been verified this runtime."),
               "Home has not been verified this runtime.");
  assert.equal(humanizeToken(null), null);
});

// ════════════════════════════════════════════════════════════════════════════════════
// Vehicle Health
// ════════════════════════════════════════════════════════════════════════════════════

test("EKF renders OK when telemetry.ekf_ok is true (was hardcoded NO TELEM)", () => {
  const r = ekfRow(liveRow());
  assert.equal(r.value, "OK");
  assert.equal(r.state, ST.LIVE);
});

test("EKF false is a fault, and EKF absent is unmeasured — three distinct states", () => {
  assert.equal(ekfRow({ telemetry: { ekf_ok: false } }).state, ST.FAULT);
  assert.equal(ekfRow({ telemetry: {} }).state, ST.UNMEASURED);
  assert.equal(ekfRow({ telemetry: {} }).value, null);
});

test("MAVLink connected renders Connected", () => {
  const r = mavlinkRow(liveRow());
  assert.equal(r.value, "Connected");
  assert.equal(r.state, ST.LIVE);
});

test("a null msg_rate_hz does NOT make MAVLink unavailable", () => {
  const r = mavlinkRow({ mavlink: { connected: true, msg_rate_hz: null, heartbeat_age_s: 0.44 } });
  assert.equal(r.value, "Connected");
  assert.match(r.detail, /heartbeat 0\.44 s ago/);
});

test("MAVLink disconnected is a fault, not an absence", () => {
  assert.equal(mavlinkRow({ mavlink: { connected: false } }).state, ST.FAULT);
  assert.equal(mavlinkRow({ mavlink: {} }).state, ST.UNMEASURED);
});

test("home verification follows `verified` even though a home position exists", () => {
  const r = homeVerificationRow(liveRow());
  assert.equal(r.value, "Not verified");
  assert.equal(r.state, ST.FAULT);
  assert.match(r.detail, /Set Home/i);
});

test("home verification renders Verified only when Scout says verified", () => {
  assert.equal(homeVerificationRow({ home: { verified: true } }).value, "Verified");
  assert.equal(homeVerificationRow({ home: {} }).value, "Unknown");
});

test("current waypoint shows Scout's continuous 0 / 15, not NOT FETCHED", () => {
  const r = currentWaypointRow(liveRow(), null);
  assert.equal(r.value, "0 / 15");
  assert.equal(r.state, ST.LIVE);
});

test("waypoint 0 is a real waypoint, not a missing value", () => {
  const r = currentWaypointRow({ mission_status: { current_waypoint: 0, mission_count: 8 } });
  assert.equal(r.value, "0 / 8");
});

test("mission_count 15 means a mission IS loaded", () => {
  assert.equal(missionLoadedRow(liveRow()).value, "Yes · 15 items");
  assert.equal(missionLoadedRow({ mission_status: { mission_count: 1 } }).value, "Yes · 1 item");
  assert.equal(missionLoadedRow({ mission_status: { mission_count: 0 } }).value, "No");
  assert.equal(missionLoadedRow({ mission_status: {} }).value, null);
});

test("a null readback does not mean no mission — the two rows disagree on purpose", () => {
  const v = { mission_status: { mission_count: 15, readback_available: false } };
  assert.equal(missionLoadedRow(v).value, "Yes · 15 items");
  assert.equal(missionReadbackRow(v, false).value, "Not fetched");
});

// ════════════════════════════════════════════════════════════════════════════════════
// Power
// ════════════════════════════════════════════════════════════════════════════════════

test("battery voltage maps from power.battery_voltage_v", () => {
  assert.equal(batteryVoltageRow({ power: { battery_voltage_v: 23.77 } }).value, "23.77 V");
});

test("battery voltage falls back to telemetry.battery_voltage (backend does the merge)", () => {
  // The backend's power_block already prefers power.* and falls back to telemetry.*,
  // so the row the frontend sees carries the resolved value either way.
  const legacyOnly = { power: { battery_voltage_v: 23.77, reported_by: "telemetry" } };
  assert.equal(batteryVoltageRow(legacyOnly).value, "23.77 V");
  assert.equal(batteryVoltageRow({ power: {} }).value, null);
});

test("battery current maps from power.battery_current_a", () => {
  assert.equal(batteryCurrentRow({ power: { battery_current_a: 0.17 } }).value, "0.17 A");
});

test("0 A renders as 0.00 A, never as missing", () => {
  const r = batteryCurrentRow({ power: { battery_current_a: 0 } });
  assert.equal(r.value, "0.00 A");
  assert.equal(r.state, ST.LIVE);
});

test("power source renders as prose, not screaming snake case", () => {
  assert.equal(powerSourceRow({ power: { source: "PIXHAWK_BATTERY_MONITOR" } }).value,
               "Pixhawk battery monitor");
  assert.equal(powerSourceRow({ power: {} }).value, null);
});

test("battery remaining maps from power.battery_remaining_pct", () => {
  const r = batteryRemainingRow({ power: { battery_remaining_pct: 90 }, battery: 12 });
  assert.equal(r.value, "90%");
  assert.equal(r.pct, 90);
});

test("failsafe OK reads as an observation, not an exhaustive guarantee", () => {
  const r = failsafeRow(liveRow());
  assert.equal(r.value, "No active failsafe observed");
  assert.equal(r.state, ST.LIVE);
});

test("a missing or UNKNOWN failsafe never becomes OK", () => {
  assert.equal(failsafeRow({ failsafe: {} }).value, null);
  assert.equal(failsafeRow({ failsafe: {} }).state, ST.UNMEASURED);
  const unknown = failsafeRow({ failsafe: { status: "UNKNOWN" } });
  assert.equal(unknown.value, "Unknown");
  assert.equal(unknown.state, ST.UNKNOWN);
  assert.equal(failsafeRow({ failsafe: { status: "ACTIVE" } }).state, ST.FAULT);
});

// ════════════════════════════════════════════════════════════════════════════════════
// Communication (diagnostics)
// ════════════════════════════════════════════════════════════════════════════════════

test("WireGuard renders every state it can be in, and never says 'Connected'", () => {
  const wg = (status, extra = {}) =>
    wireguardRow({ link: { vpn: { interface: "wg0", status, peers: 1, ...extra } } });
  assert.equal(wg("RECENT_HANDSHAKE", { last_handshake_age_s: 97.2 }).value, "Recent handshake");
  assert.equal(wg("STALE").value, "Stale handshake");
  assert.equal(wg("NO_HANDSHAKE").value, "No handshake");
  assert.equal(wg("DOWN").value, "Interface down");
  assert.equal(wg("UNKNOWN").value, "Unknown");
  for (const s of ["RECENT_HANDSHAKE", "STALE", "NO_HANDSHAKE", "DOWN", "UNKNOWN"]) {
    assert.notEqual(wg(s).value, "Connected", "WireGuard is connectionless");
  }
});

test("WireGuard severity separates a recent handshake from no handshake at all", () => {
  const wg = (status) => wireguardRow({ link: { vpn: { status } } }).state;
  assert.equal(wg("RECENT_HANDSHAKE"), ST.LIVE);
  assert.equal(wg("STALE"), ST.UNKNOWN);
  assert.equal(wg("NO_HANDSHAKE"), ST.FAULT);
  assert.equal(wg("DOWN"), ST.FAULT);
});

test("WireGuard shows the handshake age beside the state", () => {
  const r = wireguardRow({ link: { vpn: { interface: "wg0", status: "RECENT_HANDSHAKE",
                                          last_handshake_age_s: 97.2, peers: 1 } } });
  assert.match(r.detail, /handshake 97 s ago/);
});

test("a vehicle that reports no VPN block is unsupported, not faulty", () => {
  assert.equal(wireguardRow({ link: {} }).state, ST.UNSUPPORTED);
});

test("RTT renders a measured value and stays UNMEASURED on null", () => {
  assert.equal(rttRow({ link: { rtt_ms: 42 } }).value, "42 ms");
  const missing = rttRow({ link: { rtt_ms: null } });
  assert.equal(missing.value, null);
  assert.equal(missing.label, "unmeasured");
});

test("a genuine 0 ms RTT stays 0 ms and is not treated as absent", () => {
  const r = rttRow({ link: { rtt_ms: 0 } });
  assert.equal(r.value, "0 ms");
  assert.equal(r.state, ST.LIVE);
});

test("packet loss stays unmeasured until the estimator has enough samples", () => {
  const r = packetLossRow(liveRow());
  assert.equal(r.value, null);
  assert.equal(r.label, "measuring");
  assert.match(r.detail, /4 \/ 20 samples/);
});

test("a measured 0.0% loss is shown as a real measurement", () => {
  const r = packetLossRow({ link: { packet_loss: {
    state: "MEASURED", loss_pct: 0, lost: 0, expected: 60, window_s: 120 } } });
  assert.equal(r.value, "0.0%");
  assert.equal(r.state, ST.LIVE);
});

test("a high measured loss is a fault, with the arithmetic visible", () => {
  const r = packetLossRow({ link: { packet_loss: {
    state: "MEASURED", loss_pct: 12.5, lost: 5, expected: 40, window_s: 120 } } });
  assert.equal(r.value, "12.5%");
  assert.equal(r.state, ST.FAULT);
  assert.match(r.detail, /5 of 40 in 120 s/);
});

test("no packet-loss block at all is unmeasured, never 0%", () => {
  assert.equal(packetLossRow({ link: {} }).value, null);
  assert.equal(packetLossRow({}).value, null);
});

test("operator connected is driven by communication.operator_connected", () => {
  assert.equal(operatorConnectedRow({ link: { operator_connected: true } }).value, "Yes");
  assert.equal(operatorConnectedRow({ link: { operator_connected: false } }).value, "No");
  assert.equal(operatorConnectedRow({ link: { operator_connected: false } }).state, ST.FAULT);
});

test("operator connected falls back to operator_reachable and says so", () => {
  const r = operatorConnectedRow({ link: { operator_reachable: true } });
  assert.equal(r.value, "Yes");
  assert.equal(r.detail, "endpoint reachable");
  assert.equal(operatorConnectedRow({ link: {} }).value, null);
});

// ════════════════════════════════════════════════════════════════════════════════════
// Local Agent — the "[object Object]" bug
// ════════════════════════════════════════════════════════════════════════════════════

test("a string policy renders as readable prose", () => {
  const r = agentRows({ agent_summary: { current_policy: "FULL_REPORTING" } });
  assert.equal(r.policy.value, "Full reporting");
});

test("an OBJECT policy never renders [object Object]", () => {
  // The exact shape the Local Agent's POST carries.
  const structured = {
    communication_policy: "FULL_REPORTING", mission_policy: "SUPERVISED_CONTINUATION",
    autonomy_level: "ASSISTED", current_behaviour: "monitoring",
  };
  for (const v of [
    { agent_summary: { current_policy: structured } },
    { agent_status: { current_policy: structured } },
  ]) {
    const r = agentRows(v);
    for (const key of Object.keys(r)) {
      assert.equal(String(r[key].value).includes("[object Object]"), false,
                   `${key} coerced an object`);
    }
  }
});

test("current behaviour is found even when nested inside the policy object", () => {
  const r = agentRows({ agent_status: { current_policy: {
    communication_policy: "FULL_REPORTING", current_behaviour: "monitoring" } } });
  assert.equal(r.behaviour.value, "Monitoring");
});

test("current behaviour renders Monitoring from the flattened summary", () => {
  const r = agentRows(liveRow());
  assert.equal(r.behaviour.value, "Monitoring");
  assert.equal(r.decision.value, "Hold Position");
  assert.equal(r.policy.value, "Full reporting");
  assert.equal(r.autonomy.value, "Assisted");
});

test("a decision reason Scout wrote as a sentence is not recased", () => {
  const r = agentRows(liveRow());
  assert.equal(r.reason.value, "No mission assigned; standing by.");
});

test("absent agent fields are unmeasured, not blank strings", () => {
  const r = agentRows({});
  for (const key of Object.keys(r)) {
    assert.equal(r[key].value, null, key);
    assert.equal(r[key].state, ST.UNMEASURED, key);
  }
});

// ════════════════════════════════════════════════════════════════════════════════════
// Sensors
// ════════════════════════════════════════════════════════════════════════════════════

test("GPS satellites maps from telemetry.gps_satellites", () => {
  assert.equal(gpsSatellitesRow({ telemetry: { gps_satellites: 22 } }).value, "22");
  assert.equal(gpsSatellitesRow({ telemetry: { gps_satellites: 22 } }).state, ST.LIVE);
});

test("0 satellites is a real (bad) reading, not an absence", () => {
  const r = gpsSatellitesRow({ telemetry: { gps_satellites: 0 } });
  assert.equal(r.value, "0");
  assert.equal(r.state, ST.FAULT);
  assert.equal(gpsSatellitesRow({ telemetry: {} }).value, null);
});

test("IMU renders the health summary, never the raw IMU object", () => {
  const r = imuRow(liveRow());
  assert.equal(r.value, "OK");
  assert.equal(String(r.value).includes("[object Object]"), false);
  assert.match(r.detail, /s ago/);
});

test("IMU warning / stale / unknown stay distinct", () => {
  assert.equal(imuRow({ imu: { health: "WARNING" } }).state, ST.FAULT);
  assert.equal(imuRow({ imu: { health: "STALE" } }).state, ST.LAST_KNOWN);
  assert.equal(imuRow({ imu: { health: "UNKNOWN" } }).state, ST.UNKNOWN);
  assert.equal(imuRow({ imu: {} }).state, ST.UNMEASURED);
});

test("an uncalibrated leak sensor does NOT render safe, and does NOT render no-telem", () => {
  const r = leakSensorRow(liveRow());
  assert.equal(r.value, "Uncalibrated");
  assert.equal(r.state, ST.UNKNOWN);
  assert.notEqual(r.value, "No leak");
  assert.notEqual(r.value, "Safe");
  assert.notEqual(r.value, null, "telemetry exists — this is not a no-telem row");
});

test("a real leak beats everything; a calibrated dry sensor may say no leak", () => {
  assert.equal(leakSensorRow({ leak_sensor: { state: "LEAK" } }).value, "LEAK DETECTED");
  assert.equal(leakSensorRow({ leak_sensor: { state: "LEAK" } }).state, ST.FAULT);
  assert.equal(leakSensorRow({ leak_sensor: { state: "NO_LEAK" } }).value, "No leak");
  assert.equal(leakSensorRow({ leak_sensor: { state: "UNREPORTED" } }).value, null);
});

test("sonar reports the specific reason it is idle rather than a generic no-telem", () => {
  const r = bathymetryRow(liveRow());
  assert.equal(r.value, "Sampling disabled");
  assert.equal(bathymetryRow({ sampling: { has_reading: true } }).value, "Logging");
  assert.equal(bathymetryRow({}).value, null);
});

test("camera is unsupported by this vehicle rather than 'offline'", () => {
  const r = cameraRow(liveRow());
  assert.equal(r.state, ST.UNSUPPORTED);
  assert.match(r.label, /not reported by this vehicle/);
});

// ════════════════════════════════════════════════════════════════════════════════════
// System
// ════════════════════════════════════════════════════════════════════════════════════

test("Pi temperature maps from health.temperature", () => {
  assert.equal(temperatureRow({ health: { temperature: 43 } }).value, "43.0 °C");
  assert.equal(temperatureRow({ health: {} }).value, null);
});

test("service status is summarized, never rendered as an object", () => {
  const r = serviceStatusRow(liveRow());
  assert.equal(r.value, "5 online · 1 unknown");
  assert.equal(r.state, ST.UNKNOWN, "an unknown OPTIONAL service is not a failure");
  assert.equal(String(r.value).includes("[object Object]"), false);
});

test("all services online summarizes to Nominal", () => {
  const r = serviceStatusRow({ service_status: {
    reported: true, services: { a: "ONLINE", b: "ONLINE" },
    online: ["a", "b"], offline: [], unknown: [], required_offline: [] } });
  assert.equal(r.value, "Nominal");
  assert.equal(r.state, ST.LIVE);
});

test("a required service offline is called out specifically", () => {
  const r = serviceStatusRow({ service_status: {
    reported: true, services: { pixhawk_link: "OFFLINE", vehicle_api: "ONLINE" },
    online: ["vehicle_api"], offline: ["pixhawk_link"], unknown: [],
    required_offline: ["pixhawk_link"] } });
  assert.equal(r.value, "1 required offline");
  assert.equal(r.state, ST.FAULT);
  assert.deepEqual(r.names, ["pixhawk_link"]);
});

// ════════════════════════════════════════════════════════════════════════════════════
// No row anywhere can produce [object Object], and none of the fixed rows may be
// hardcoded back to a placeholder.
// ════════════════════════════════════════════════════════════════════════════════════

const ALL_ROWS = [
  ekfRow, mavlinkRow, homeVerificationRow, currentWaypointRow, missionLoadedRow,
  missionReadbackRow, batteryVoltageRow, batteryCurrentRow, powerSourceRow,
  batteryRemainingRow, failsafeRow, wireguardRow, rttRow, packetLossRow,
  operatorConnectedRow, gpsSatellitesRow, imuRow, leakSensorRow, bathymetryRow,
  cameraRow, temperatureRow, serviceStatusRow,
];

test("no derivation coerces an object, however hostile the input", () => {
  const hostile = {
    telemetry: { ekf_ok: { value: true }, gps_satellites: { n: 22 } },
    power: { battery_voltage_v: { v: 23 }, source: { name: "PIXHAWK" } },
    failsafe: { status: { code: "OK" } },
    imu: { health: { state: "OK" } },
    mavlink: { connected: { yes: true } },
    health: { temperature: { c: 43 } },
    mission_status: { current_waypoint_display: { text: "0 / 15" }, mission_count: {} },
    home: { verified: { v: false }, reason: { message: "not verified" } },
    link: { rtt_ms: { ms: 40 }, vpn: { status: { s: "DOWN" } },
            packet_loss: { state: "MEASURED", loss_pct: { p: 5 } } },
    leak_sensor: { state: { s: "LEAK" } },
    service_status: { reported: true, services: { a: { s: "ONLINE" } } },
    sampling: { enabled: { e: false } },
  };
  for (const fn of ALL_ROWS) {
    const r = fn(hostile);
    for (const key of ["value", "label", "detail"]) {
      assert.equal(String(r[key]).includes("[object Object]"), false,
                   `${fn.name}.${key} coerced an object`);
    }
  }
});

test("every derivation survives an empty vehicle without throwing", () => {
  for (const fn of ALL_ROWS) {
    const r = fn({});
    assert.ok(r && typeof r.state === "string", fn.name);
  }
});

test("the Vehicle page renders the fixed rows from the derivations, not placeholders", () => {
  // Each of these was a hardcoded naRow() over data Scout was sending.
  for (const [label, deriver] of [
    ["EKF", "vt.ekfRow"],
    ["Battery voltage", "vt.batteryVoltageRow"],
    ["Battery current", "vt.batteryCurrentRow"],
    ["Power source", "vt.powerSourceRow"],
    ["Failsafe status", "vt.failsafeRow"],
    ["WireGuard", "vt.wireguardRow"],
    ["MAVLink", "vt.mavlinkRow"],
    ["Packet loss", "vt.packetLossRow"],
    ["RTT", "vt.rttRow"],
    ["GPS satellites", "vt.gpsSatellitesRow"],
    ["IMU", "vt.imuRow"],
    ["Camera", "vt.cameraRow"],
    ["Leak sensor", "vt.leakSensorRow"],
    ["Temperature", "vt.temperatureRow"],
    ["Service status", "vt.serviceStatusRow"],
  ]) {
    assert.match(vehicleCode, new RegExp(`diagRow\\("${label}",\\s*${deriver.replace(".", "\\.")}`),
                 `"${label}" is no longer derived from ${deriver}`);
  }
});

test("the Vehicle page no longer stringifies the agent policy", () => {
  assert.equal(/String\(\s*a\.current_policy/.test(vehicleCode), false);
  assert.equal(/clean\(a\.current_policy/.test(vehicleCode), false);
});

test("the Vehicle page no longer claims a nominal leak sensor from a null", () => {
  assert.equal(/h\.leak_detected/.test(vehicleCode), false,
               "health.leak_detected is always null here — it must not gate an OK");
});
