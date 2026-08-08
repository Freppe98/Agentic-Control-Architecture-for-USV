// vehicle-telemetry.js — pure derivations for the Vehicle page's diagnostic rows.
//
// WHY THIS EXISTS: the Vehicle page hardcoded ~15 rows as permanent "— NO TELEM"
// placeholders (EKF, battery voltage/current, power source, failsafe, WireGuard, RTT,
// packet loss, GPS satellites, IMU, camera, temperature, service status) while Scout was
// sending every one of them on every status packet. Others read field spellings the
// backend never produced (`health.flask_status`, `health.leak_detected`), and one
// interpolated a structured policy object straight into the DOM as "[object Object]".
//
// Every row is derived HERE, as a pure function of the fleet row, returning a
// { state, value, label, detail } record. No DOM, no fetch, no page-local field
// spellings — so the mapping is unit-testable and a second page can reuse it without
// re-deriving a slightly different judgement about the same reading.
//
// HONESTY RULES (DATA_DICTIONARY.md → Data Availability States):
//   • `null`/`undefined` is NOT-OBSERVED. `0` is a reading. Nothing here uses a bare
//     truthiness test on a numeric field — 0 A, 0 ms, waypoint 0 and 0 % are all real.
//   • states are distinguished, never collapsed into one "—":
//       live         a current reading
//       last_known   a real reading that is no longer current (link degraded)
//       unmeasured   the metric exists but has not been measured yet
//       unsupported  this vehicle/platform does not report it at all
//       unknown      reported, but the value itself says "unknown"
//       fault        reported, and the value says something is wrong
//   • an UNKNOWN is never upgraded to an OK, and an uncalibrated sensor is never
//     rendered as SAFE.
import { asText } from "./format.js";

export const ST = {
  LIVE: "live",
  LAST_KNOWN: "last_known",
  UNMEASURED: "unmeasured",
  UNSUPPORTED: "unsupported",
  UNKNOWN: "unknown",
  FAULT: "fault",
};

/** A real number, or null. Bools are not numbers; 0 passes through. */
export function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
/** True / false / null (not observed). Anything non-boolean is "not observed". */
export function tri(v) {
  return typeof v === "boolean" ? v : null;
}
/** Explicit presence test — the replacement for `if (value)` on a numeric field. */
export const present = (v) => v !== null && v !== undefined;

/**
 * A single machine token → sentence case, e.g.
 *   PIXHAWK_BATTERY_MONITOR → "Pixhawk battery monitor"
 *   RECENT_HANDSHAKE        → "Recent handshake"
 *   monitoring              → "Monitoring"
 * Only SINGLE tokens (no whitespace) are touched, so anything Scout wrote as prose —
 * "Hold Position", "No mission assigned; standing by." — is returned untouched rather
 * than being recased into something it did not say.
 */
export function humanizeToken(value) {
  const text = asText(value);
  if (text === null) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(text)) return text;
  const words = text.replace(/[_-]+/g, " ").toLowerCase().trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const row = (state, value, extra = {}) => ({ state, value, label: null, detail: null, ...extra });

// ── Vehicle Health ────────────────────────────────────────────────────────────────

/**
 * EKF filter health from `telemetry.ekf_ok`.
 * Was hardcoded to NO TELEM while Scout sent `ekf_ok: true` on every packet.
 * `false` is a real fault, not an absence — the three cases are kept apart.
 */
export function ekfRow(v) {
  const ok = tri((v && v.telemetry && v.telemetry.ekf_ok));
  if (ok === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  return ok ? row(ST.LIVE, "OK") : row(ST.FAULT, "Not OK", { label: "EKF unhealthy" });
}

/**
 * MAVLink (Pixhawk↔Pi USB link) availability.
 * `mavlink_connected` is the PRIMARY test; `msg_rate_hz` is supporting detail and is
 * frequently null on a perfectly live link — using it as the availability test is how
 * a connected autopilot rendered as NO TELEM.
 */
export function mavlinkRow(v) {
  const mav = (v && v.mavlink) || {};
  const connected = tri(mav.connected);
  const rate = num(mav.msg_rate_hz);
  const hb = num(mav.heartbeat_age_s);
  const detail = [
    hb !== null ? `heartbeat ${hb.toFixed(2)} s ago` : null,
    rate !== null ? `${rate} Hz` : null,
  ].filter(Boolean).join(" · ") || null;
  if (connected === true) return row(ST.LIVE, "Connected", { detail });
  if (connected === false) return row(ST.FAULT, "Disconnected", { detail });
  // No explicit flag: message age is still real evidence the link carried something.
  const lastMsg = num(mav.last_msg_age_s);
  if (lastMsg !== null) return row(ST.UNKNOWN, `Last message ${lastMsg.toFixed(2)} s ago`, { detail });
  return row(ST.UNMEASURED, null, { label: "not reported" });
}

/**
 * Home verification — `agent.home_status.verified`, mirrored onto the fleet row's
 * `home` block, and NOTHING ELSE.
 *
 * The existence of a HOME_POSITION on the Pixhawk is NOT verification: Scout currently
 * reports a home ~1.6 km from the vehicle with `verified: false`, because nobody has
 * confirmed it for this deployment site this runtime. Inferring VERIFIED from
 * `home_position != null` would arm AUTO/RTL against a recovery point in another town.
 */
export function homeVerificationRow(v) {
  const home = (v && v.home) || {};
  const verified = tri(home.verified);
  const reason = asText(home.reason);
  if (verified === true) return row(ST.LIVE, "Verified", { detail: reason });
  if (verified === false) {
    return row(ST.FAULT, "Not verified", {
      label: "set home required",
      detail: reason || "Set Home at the deployment site before AUTO/RTL/RESUME.",
    });
  }
  return row(ST.UNKNOWN, "Unknown", { detail: reason });
}

/**
 * Current waypoint — Scout's own `current_waypoint_display` ("0 / 15"), which it reports
 * every packet from MISSION_CURRENT/MISSION_COUNT.
 *
 * This row used to be answered ONLY by the operator's separate /pixhawk_mission proxy
 * fetch, so it said "NOT FETCHED" for a mission Scout was actively describing. The
 * readback is for drawing and hashing the ROUTE; it is not needed to say which item is
 * current. Falls back to the operator readback, then to the raw index.
 */
export function currentWaypointRow(v, readback = null) {
  const m = (v && v.mission_status) || {};
  const display = asText(m.current_waypoint_display);
  if (display) return row(ST.LIVE, display);
  const cur = num(m.current_waypoint);
  const total = num(m.mission_count);
  if (cur !== null && total !== null) return row(ST.LIVE, `${cur} / ${total}`);
  if (cur !== null) return row(ST.LIVE, String(cur));
  if (readback && num(readback.cur) !== null) return row(ST.LIVE, String(num(readback.cur)));
  return row(ST.UNMEASURED, null, { label: "not reported" });
}

/**
 * Mission loaded — presence of a mission ON THE AUTOPILOT, from `mission_count`.
 * Kept strictly separate from whether the operator has fetched the full readback:
 * "Yes · 15 items" plus "Readback: not fetched" are both true at the same time, and
 * collapsing them made a loaded mission read as no mission at all.
 */
export function missionLoadedRow(v) {
  const m = (v && v.mission_status) || {};
  const count = num(m.mission_count);
  if (count === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  if (count <= 0) return row(ST.LIVE, "No");
  return row(ST.LIVE, `Yes · ${count} item${count === 1 ? "" : "s"}`);
}

/** Readback status — the operator-side companion to missionLoadedRow. */
export function missionReadbackRow(v, fetched = false) {
  const m = (v && v.mission_status) || {};
  if (fetched) return row(ST.LIVE, "Fetched");
  if (m.readback_available === true) {
    const age = num(m.readback && m.readback.age_s);
    return row(ST.LIVE, "Available on vehicle",
      { detail: age !== null ? `read ${age.toFixed(1)} s ago` : null });
  }
  return row(ST.UNMEASURED, "Not fetched", { label: "operator readback" });
}

// ── Power ─────────────────────────────────────────────────────────────────────────

function powerNumberRow(v, field, unit, decimals) {
  const value = num(((v && v.power) || {})[field]);
  if (value === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  return row(ST.LIVE, `${value.toFixed(decimals)} ${unit}`);
}

/** Battery voltage — `power.battery_voltage_v`, backend-fallback `telemetry.battery_voltage`. */
export function batteryVoltageRow(v) { return powerNumberRow(v, "battery_voltage_v", "V", 2); }

/** Battery current — `power.battery_current_a`. A genuine 0.00 A renders as 0.00 A. */
export function batteryCurrentRow(v) { return powerNumberRow(v, "battery_current_a", "A", 2); }

/** Power source — `power.source`, rendered as prose rather than a shouted token. */
export function powerSourceRow(v) {
  const source = humanizeToken(((v && v.power) || {}).source);
  if (source === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  return row(ST.LIVE, source);
}

/** Remaining % — `power.battery_remaining_pct` (the backend already treats -1 as absence). */
export function batteryRemainingRow(v) {
  const pct = num(((v && v.power) || {}).battery_remaining_pct);
  const fallback = num(v && v.battery);
  const value = pct !== null ? pct : fallback;
  if (value === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  return row(ST.LIVE, `${value}%`, { pct: value });
}

/**
 * Failsafe — `failsafe.status`.
 *
 * The wording matters. "OK" here means Scout observed no ACTIVE failsafe condition; it
 * is not a claim that every ArduPilot failsafe subsystem has been exhaustively verified,
 * so the row says "No active failsafe observed" rather than a bare "OK". A missing or
 * UNKNOWN status stays unknown — silence is never upgraded to nominal.
 */
export function failsafeRow(v) {
  const fs = (v && v.failsafe) || {};
  const status = asText(fs.status);
  const unhealthy = tri(fs.unhealthy_sensors_present);
  // `system_state` is MAV_STATE (ACTIVE = the autopilot is running), which is deliberately
  // NOT shown here: "No active failsafe observed · system Active" reads as though a
  // failsafe were active. Only evidence that actually qualifies the failsafe verdict
  // belongs beside it.
  const detail = unhealthy === true ? "unhealthy sensors reported" : null;
  if (!status) return row(ST.UNMEASURED, null, { label: "not reported" });
  const token = status.toUpperCase();
  if (token === "OK" || token === "NONE" || token === "CLEAR") {
    return row(ST.LIVE, "No active failsafe observed", { detail });
  }
  if (token === "UNKNOWN") return row(ST.UNKNOWN, "Unknown", { detail });
  if (token === "ACTIVE") return row(ST.FAULT, "Active", { detail });
  return row(ST.FAULT, humanizeToken(status), { detail });
}

// ── Communication (diagnostics — never the comm-state verdict) ────────────────────

/**
 * WireGuard — `communication.vpn_status`.
 *
 * WireGuard is connectionless: there is no session to be "connected" to, only an
 * interface that is up or down and a handshake that is recent or not. The status token
 * is rendered as-is with the handshake age beside it; it is never relabelled
 * "Connected".
 */
export function wireguardRow(v) {
  const vpn = ((v && v.link) || {}).vpn;
  if (!vpn) return row(ST.UNSUPPORTED, null, { label: "not reported" });
  const status = asText(vpn.status);
  const token = status ? status.toUpperCase() : "UNKNOWN";
  const age = num(vpn.last_handshake_age_s);
  const iface = asText(vpn.interface);
  const detail = [
    iface,
    age !== null ? `handshake ${Math.round(age)} s ago` : null,
    num(vpn.peers) !== null ? `${num(vpn.peers)} peer${num(vpn.peers) === 1 ? "" : "s"}` : null,
  ].filter(Boolean).join(" · ") || null;

  switch (token) {
    case "RECENT_HANDSHAKE": return row(ST.LIVE, "Recent handshake", { detail });
    case "STALE":            return row(ST.UNKNOWN, "Stale handshake", { detail });
    case "NO_HANDSHAKE":     return row(ST.FAULT, "No handshake", { detail });
    case "DOWN":             return row(ST.FAULT, "Interface down", { detail });
    case "UNKNOWN":          return row(ST.UNKNOWN, "Unknown", { detail });
    default:                 return row(ST.UNKNOWN, humanizeToken(token), { detail });
  }
}

/**
 * Application round-trip time — `communication.rtt_ms`: how long the vehicle's own
 * POST /agent/status to this operator took, measured by the Local Agent. This is the
 * 4G/WireGuard path, deliberately NOT the Pixhawk USB link.
 * A null is UNMEASURED; a genuine 0 stays 0.
 */
export function rttRow(v) {
  const rtt = num(((v && v.link) || {}).rtt_ms);
  if (rtt === null) return row(ST.UNMEASURED, null, { label: "unmeasured" });
  return row(ST.LIVE, `${Math.round(rtt)} ms`);
}

/**
 * Packet loss — the OPERATOR's own estimate over the Local Agent's `communication.seq`
 * (backend `link.packet_loss`). Stays UNMEASURED until enough samples exist; it is never
 * a fabricated 0 %, and an agent restart or a reconnection reports "measuring", not 100 %.
 */
export function packetLossRow(v) {
  const pl = ((v && v.link) || {}).packet_loss;
  if (!pl) return row(ST.UNMEASURED, null, { label: "unmeasured" });
  const pct = num(pl.loss_pct);
  // `num` (not a truthiness or presence test) is the guard: a null loss_pct means the
  // estimator has not measured yet, and a real 0 must still get through.
  if (pl.state !== "MEASURED" || pct === null) {
    const have = num(pl.samples) || 0;
    const need = num(pl.min_samples) || 0;
    return row(ST.UNMEASURED, null,
      { label: "measuring", detail: need ? `${have} / ${need} samples` : null });
  }
  const detail = `${num(pl.lost) ?? "?"} of ${num(pl.expected) ?? "?"} in ${Math.round(num(pl.window_s) || 0)} s`;
  if (pct === 0) return row(ST.LIVE, "0.0%", { detail });
  return row(pct >= 5 ? ST.FAULT : ST.LIVE, `${pct.toFixed(1)}%`, { detail });
}

/**
 * Operator connected — the vehicle's canonical `communication.operator_connected`.
 * NOT inferred from control authority (a different question entirely), and NOT from
 * whether a browser is open. Falls back to `operator_reachable`, which is the weaker
 * "the endpoint answers" claim, and says which one answered.
 */
export function operatorConnectedRow(v) {
  const link = (v && v.link) || {};
  const connected = tri(link.operator_connected);
  if (connected !== null) {
    return connected ? row(ST.LIVE, "Yes") : row(ST.FAULT, "No");
  }
  const reachable = tri(link.operator_reachable);
  if (reachable !== null) {
    return reachable
      ? row(ST.LIVE, "Yes", { detail: "endpoint reachable" })
      : row(ST.FAULT, "No", { detail: "endpoint unreachable" });
  }
  return row(ST.UNMEASURED, null, { label: "not reported" });
}

// ── Local Agent ───────────────────────────────────────────────────────────────────

/**
 * The Local Agent's reasoning rows. The backend already flattens Scout's structured
 * `current_policy` object; this is the second line of defence — every value goes
 * through asText(), so an object can never reach the DOM as "[object Object]" even if
 * a future Local Agent nests something new.
 */
export function agentRows(v) {
  const a = (v && v.agent_summary) || {};
  const legacy = (v && v.agent_status) || {};
  // The Local Agent nests these INSIDE current_policy. The backend already flattens it;
  // reading the nested object here too means a station talking to an older backend, or a
  // future Local Agent that nests something new, still renders text rather than "[object
  // Object]" or a spurious "not emitted".
  const nested = (legacy.current_policy && typeof legacy.current_policy === "object"
                  && !Array.isArray(legacy.current_policy)) ? legacy.current_policy : {};
  const pick = (...candidates) => {
    for (const c of candidates) {
      if (c !== null && typeof c === "object") continue;   // never coerce a structure
      const t = asText(c);
      if (t !== null) return t;
    }
    return null;
  };
  const behaviour = pick(a.current_behaviour, legacy.current_behaviour, legacy.behaviour,
                         nested.current_behaviour);
  const decision = pick(a.current_decision, legacy.current_decision);
  const policy = pick(a.current_policy, a.communication_policy, legacy.current_policy,
                      nested.communication_policy, nested.policy, nested.value, nested.name);
  const reason = pick(a.decision_reason,
    Array.isArray(legacy.decision_reasons) ? legacy.decision_reasons[0] : legacy.decision_reasons,
    legacy.decision_reason);
  const mission = pick(a.mission_policy, nested.mission_policy);
  const autonomy = pick(a.autonomy_level, legacy.autonomy_level, nested.autonomy_level);
  const mk = (value) => (value === null
    ? row(ST.UNMEASURED, null, { label: "not reported" })
    : row(ST.LIVE, humanizeToken(value)));
  return {
    behaviour: mk(behaviour),
    decision: mk(decision),
    // A policy is a token; the reason is a sentence Scout wrote and must not be recased.
    policy: policy === null ? row(ST.UNMEASURED, null, { label: "not reported" })
                            : row(ST.LIVE, humanizeToken(policy),
                                  { detail: mission === null ? null : `mission ${humanizeToken(mission)}` }),
    reason: reason === null ? row(ST.UNMEASURED, null, { label: "not reported" })
                            : row(ST.LIVE, reason),
    autonomy: autonomy === null ? row(ST.UNMEASURED, null, { label: "not reported" })
                                : row(ST.LIVE, humanizeToken(autonomy)),
  };
}

// ── Sensors ───────────────────────────────────────────────────────────────────────

/** GPS satellites — `telemetry.gps_satellites`. 0 satellites is a real (bad) reading. */
export function gpsSatellitesRow(v) {
  const sats = num(((v && v.telemetry) || {}).gps_satellites);
  if (sats === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  const state = sats === 0 ? ST.FAULT : sats < 6 ? ST.UNKNOWN : ST.LIVE;
  return row(state, String(sats));
}

/**
 * IMU — the health SUMMARY (`imu.imu_health`), not the raw attitude/vibration/clipping
 * dictionary. Dumping that object into a status row is how objects reach the DOM.
 */
export function imuRow(v) {
  const imu = (v && v.imu) || {};
  const health = asText(imu.health);
  const age = num(imu.last_seen_s);
  const detail = age !== null ? `${age.toFixed(2)} s ago` : null;
  if (!health) {
    if (tri(imu.available) === false) return row(ST.FAULT, "Unavailable");
    return row(ST.UNMEASURED, null, { label: "not reported" });
  }
  const token = health.toUpperCase();
  if (token === "OK") return row(ST.LIVE, "OK", { detail });
  if (token === "UNKNOWN") return row(ST.UNKNOWN, "Unknown", { detail });
  if (token === "WARNING" || token === "WARN") return row(ST.FAULT, "Warning", { detail });
  if (token === "STALE") return row(ST.LAST_KNOWN, "Stale", { detail });
  return row(ST.UNKNOWN, humanizeToken(health), { detail });
}

/**
 * Leak sensor — `leak_sensor.state`.
 *
 * UNCALIBRATED is the important case and the current one: the pin reads LOW, but nobody
 * has established whether LOW means dry or flooded, so `leak_detected` is null. This
 * must NOT render "Safe" (a dangerous lie), must NOT render "Leak" (a false alarm), and
 * must NOT render "no telem" (telemetry plainly exists).
 */
export function leakSensorRow(v) {
  const leak = (v && v.leak_sensor) || {};
  const signal = asText(leak.signal);
  switch (leak.state) {
    case "LEAK":
      return row(ST.FAULT, "LEAK DETECTED");
    case "NO_LEAK":
      return row(ST.LIVE, "No leak", { detail: signal ? `signal ${signal}` : null });
    case "UNCALIBRATED":
      return row(ST.UNKNOWN, "Uncalibrated", {
        label: "polarity unknown",
        detail: signal ? `sensor readable · signal ${signal}` : "sensor readable",
      });
    case "UNAVAILABLE":
      return row(ST.FAULT, "Unavailable");
    default:
      return row(ST.UNMEASURED, null, { label: "not reported" });
  }
}

/**
 * Sonar / bathymetry. Scout reports whether sampling is ENABLED, which is provable
 * evidence of an idle payload — far more useful than a generic NO TELEM, which reads
 * as "the station is blind" rather than "the sensor is switched off".
 */
export function bathymetryRow(v) {
  const s = (v && v.sampling) || {};
  const services = ((v && v.service_status) || {}).services || {};
  const sensorService = services.sensor_service;
  if (s.has_reading === true) return row(ST.LIVE, "Logging");
  if (tri(s.enabled) === false) {
    return row(ST.UNKNOWN, "Sampling disabled", {
      detail: sensorService === "ONLINE" ? "sensor service online" : null,
    });
  }
  if (tri(s.enabled) === true) return row(ST.UNMEASURED, "Enabled · no reading yet");
  return row(ST.UNMEASURED, null, { label: "not reported" });
}

/**
 * Camera. Scout reports no camera field at all — not a service, not a health entry.
 * That is UNSUPPORTED ("not reported by this vehicle"), which is a different and more
 * useful statement than "no telemetry", and it must not be invented into an
 * "Offline" that implies a camera exists and has failed.
 */
export function cameraRow(v) {
  const services = ((v && v.service_status) || {}).services || {};
  const state = services.camera || services.camera_service;
  if (!state) return row(ST.UNSUPPORTED, null, { label: "not reported by this vehicle" });
  if (state === "ONLINE") return row(ST.LIVE, "Online");
  return row(ST.FAULT, humanizeToken(state));
}

// ── System ────────────────────────────────────────────────────────────────────────

/** Pi temperature — `health.temperature`, in °C. */
export function temperatureRow(v) {
  const t = num(((v && v.health) || {}).temperature);
  if (t === null) return row(ST.UNMEASURED, null, { label: "not reported" });
  const state = t >= 80 ? ST.FAULT : ST.LIVE;
  return row(state, `${t.toFixed(1)} °C`);
}

/**
 * Service status — a one-line SUMMARY of the `service_status` object, never the object.
 * An unknown OPTIONAL service (influx, a logging sink) does not make the vehicle
 * unhealthy, so it is reported as a qualifier rather than a failure.
 */
export function serviceStatusRow(v) {
  const svc = (v && v.service_status) || {};
  if (!svc.reported) return row(ST.UNMEASURED, null, { label: "not reported" });
  const offline = svc.offline || [];
  const requiredOffline = svc.required_offline || [];
  const unknown = svc.unknown || [];
  const online = svc.online || [];
  // The per-service breakdown goes in the TOOLTIP, not inline: six "name: state" pairs
  // on one status line is the object-dump problem in a politer form. The summary answers
  // "is anything wrong?" at a glance; the hover answers "which one?".
  const detail = Object.entries(svc.services || {})
    .map(([name, state]) => `${name}: ${String(state).toLowerCase()}`).join(", ");
  const opts = { detail, detailTooltipOnly: true };
  if (requiredOffline.length) {
    return row(ST.FAULT, `${requiredOffline.length} required offline`,
      { ...opts, names: requiredOffline });
  }
  if (offline.length) {
    return row(ST.FAULT, `${offline.length} offline`, { ...opts, names: offline });
  }
  if (unknown.length) {
    return row(ST.UNKNOWN, `${online.length} online · ${unknown.length} unknown`, opts);
  }
  return row(ST.LIVE, "Nominal", opts);
}
