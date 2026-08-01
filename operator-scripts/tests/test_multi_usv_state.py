"""Multi-USV current-state isolation — the fix for the live two-vehicle alternation bug.

Symptom: with Scout (usv-2) and SAR-001 (usv-3) both posting to the same Operator Station,
every page alternated between fleet states every couple of seconds — whichever vehicle
posted last was the only fully populated row, and the other reverted to a static UNKNOWN
placeholder (which is also why the same vehicle appeared as "SAR-001" one second and
"USV-3" the next).

Cause: ONE global `latest_agent_status` + ONE global `latest_agent_received_at`, spliced
into a hardcoded FLEET_TEMPLATE by GET /api/fleet/status. Every vehicle's row — including
its comm-state — was a function of the single most recent packet from ANY vehicle.

These tests pin the invariant that replaced it: every USV owns one independent record
keyed by canonical id, and a packet from vehicle A can never change vehicle B.
"""
import unittest
from datetime import datetime, timedelta, timezone

import main
from fastapi.testclient import TestClient

SCOUT = 2          # canonical id — display name "Scout"
SAR = 3            # canonical id — display name "SAR-001"
THIRD = 4          # a third USV, not in the registry: discovered purely from its packets


def packet(usv_id, *, ts, name=None, battery=None, mode=None, lat=None, lng=None,
           mission_state=None, heading=None, groundspeed=None, agent=None, health=None,
           authority=None, source=None, telemetry=True):
    """One status envelope in the shape a Local Agent posts."""
    payload = {"usv_id": usv_id, "comm_state": "CONNECTED"}
    if name is not None:
        payload["name"] = name
    if telemetry:
        tel = {}
        for key, val in (("battery", battery), ("mode", mode), ("lat", lat), ("lng", lng),
                         ("heading", heading), ("groundspeed", groundspeed)):
            if val is not None:
                tel[key] = val
        payload["telemetry"] = tel
    if mission_state is not None:
        payload["mission"] = {"mission_state": mission_state}
    if agent is not None:
        payload["agent"] = agent
    if health is not None:
        payload["health"] = health
    if authority is not None:
        payload["agent"] = {**(payload.get("agent") or {}), "control_authority": authority}
    return {"message_type": "status", "schema_version": "1.0",
            "source": source or f"usv-{usv_id}", "target": "operator", "timestamp": ts,
            "payload": payload}


class MultiUsvBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.current_vehicle_state.clear()
        main.last_known_telemetry.clear()
        main.last_known_agent.clear()
        main.latest_msg_ts_by_id.clear()
        main.last_seen_by_id.clear()
        main.comms_state_by_id.clear()
        main.comms_history_by_id.clear()
        main.last_agent_decision_by_id.clear()
        main.last_mission_state_by_id.clear()
        # The command queue is module-level too: clear it so a queue left behind by another
        # test file cannot look like a command routed to the wrong vehicle here.
        main.commands.clear()
        main.commands_by_id.clear()
        main.vehicle_names = {c: main.REGISTRY.default_display_name(c)
                              for c in main.REGISTRY.configured_ids()}

    # --- helpers ---------------------------------------------------------------------
    def post(self, pkt):
        return self.client.post("/agent/status", json=pkt)

    def fleet(self):
        r = self.client.get("/api/fleet/status")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def row(self, cid):
        rows = [v for v in self.fleet() if v["id"] == cid]
        self.assertEqual(len(rows), 1, f"expected exactly one row for {cid!r}, got {len(rows)}")
        return rows[0]

    def age(self, cid, seconds):
        """Backdate ONE vehicle's last contact, leaving every other vehicle untouched."""
        when = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        main.last_seen_by_id[cid] = when
        main.current_vehicle_state[cid]["received_at"] = when


class TwoVehiclesAreIndependent(MultiUsvBase):
    """Requirements 1–6: both vehicles complete, simultaneously, under interleaving."""

    def setUp(self):
        super().setUp()
        self.post(packet(2, ts=1000, name="Scout", battery=79, mode="AUTO",
                         lat=56.70, lng=13.00, mission_state="EXECUTING"))
        self.post(packet(3, ts=1000, name="SAR-001", battery=50, mode="MANUAL",
                         lat=56.71, lng=13.01, mission_state="IDLE"))

    def test_fleet_contains_both_records_with_their_own_data(self):
        scout, sar = self.row(SCOUT), self.row(SAR)
        self.assertEqual((scout["name"], scout["battery"], scout["telemetry"]["mode"],
                          scout["comm_state"]), ("Scout", 79, "AUTO", "CONNECTED"))
        self.assertEqual((sar["name"], sar["battery"], sar["telemetry"]["mode"],
                          sar["comm_state"]), ("SAR-001", 50, "MANUAL", "CONNECTED"))

    def test_a_later_scout_packet_does_not_alter_any_sar_field(self):
        before = self.row(SAR)
        self.post(packet(2, ts=1001, name="Scout", battery=71, mode="LOITER",
                         lat=56.60, lng=12.90, mission_state="PAUSED"))
        after = self.row(SAR)
        for field in ("name", "battery", "lat", "lng", "mission", "comm_state", "telemetry"):
            self.assertEqual(before[field], after[field], f"Scout's packet changed SAR's {field}")

    def test_a_later_sar_packet_does_not_alter_any_scout_field(self):
        before = self.row(SCOUT)
        self.post(packet(3, ts=1001, name="SAR-001", battery=44, mode="HOLD",
                         lat=56.90, lng=13.90, mission_state="ABORTED"))
        after = self.row(SCOUT)
        for field in ("name", "battery", "lat", "lng", "mission", "comm_state", "telemetry"):
            self.assertEqual(before[field], after[field], f"SAR's packet changed Scout's {field}")

    def test_interleaved_packets_never_make_a_row_go_unknown(self):
        """The live symptom, reproduced: alternate Scout/SAR at 1 Hz and assert that BOTH
        rows stay complete and CONNECTED after every single packet."""
        ts = 1001
        for i in range(6):
            self.post(packet(2, ts=ts, name="Scout", battery=79 - i, mode="AUTO",
                             lat=56.70, lng=13.00, mission_state="EXECUTING"))
            ts += 1
            self.post(packet(3, ts=ts, name="SAR-001", battery=50 - i, mode="MANUAL",
                             lat=56.71, lng=13.01, mission_state="IDLE"))
            ts += 1
            scout, sar = self.row(SCOUT), self.row(SAR)
            self.assertEqual((scout["comm_state"], sar["comm_state"]), ("CONNECTED", "CONNECTED"))
            self.assertEqual((scout["battery"], sar["battery"]), (79 - i, 50 - i))
            self.assertEqual((scout["name"], sar["name"]), ("Scout", "SAR-001"))
            self.assertEqual((scout["lat"], sar["lat"]), (56.70, 56.71))

    def test_scout_battery_never_appears_on_sar(self):
        for _ in range(4):
            self.assertNotEqual(self.row(SAR)["battery"], self.row(SCOUT)["battery"])
        self.assertEqual(self.row(SAR)["battery"], 50)


class PerVehicleFreshness(MultiUsvBase):
    """Requirements 7, 8, 16: staleness is evaluated per USV, on its own clock."""

    def setUp(self):
        super().setUp()
        self.post(packet(2, ts=1000, name="Scout", battery=79, mode="AUTO", lat=56.7, lng=13.0))
        self.post(packet(3, ts=1000, name="SAR-001", battery=50, mode="MANUAL", lat=56.71, lng=13.01))

    def test_scout_can_go_stale_while_sar_stays_connected(self):
        self.age(SCOUT, main.PARTITIONED_AFTER_SECONDS + 1)
        self.assertEqual(self.row(SCOUT)["comm_state"], "PARTITIONED")
        self.assertEqual(self.row(SAR)["comm_state"], "CONNECTED")

    def test_sar_can_go_stale_while_scout_stays_connected(self):
        self.age(SAR, main.DISCONNECTED_AFTER_SECONDS + 1)
        self.assertEqual(self.row(SAR)["comm_state"], "DISCONNECTED")
        self.assertEqual(self.row(SCOUT)["comm_state"], "CONNECTED")

    def test_a_connected_vehicle_does_not_make_a_silent_one_look_connected(self):
        """The old global-received_at bug: any packet refreshed EVERY vehicle's freshness."""
        self.age(SCOUT, main.DISCONNECTED_AFTER_SECONDS + 5)
        self.post(packet(3, ts=1001, name="SAR-001", battery=49, mode="MANUAL"))
        self.assertEqual(self.row(SCOUT)["comm_state"], "DISCONNECTED")

    def test_disconnected_vehicle_stays_in_the_fleet_with_last_known_data(self):
        self.age(SCOUT, main.DISCONNECTED_AFTER_SECONDS + 5)
        scout = self.row(SCOUT)
        self.assertEqual(scout["comm_state"], "DISCONNECTED")
        self.assertEqual(scout["battery"], 79, "last-known battery must survive going stale")
        self.assertEqual((scout["lat"], scout["lng"]), (56.7, 13.0))
        self.assertIn(SCOUT, [v["id"] for v in self.fleet()])

    def test_last_contact_age_is_per_vehicle(self):
        self.age(SCOUT, 12)
        self.assertGreaterEqual(self.row(SCOUT)["last_seen_age_s"], 11)
        self.assertLess(self.row(SAR)["last_seen_age_s"], 5)


class PerVehiclePacketOrdering(MultiUsvBase):
    """Requirement 9: the monotonic guard is per USV — one vehicle's clock never gates
    another's. Interleaved arrivals from several USVs are normal traffic, not replay."""

    def setUp(self):
        super().setUp()
        self.post(packet(2, ts=2000, name="Scout", battery=79, mode="AUTO"))
        self.post(packet(3, ts=2000, name="SAR-001", battery=50, mode="MANUAL"))

    def test_old_scout_packet_is_rejected(self):
        r = self.post(packet(2, ts=1500, name="Scout", battery=10, mode="HOLD"))
        self.assertTrue(r.json()["stale"])
        self.assertEqual(self.row(SCOUT)["battery"], 79, "a replayed packet must not overwrite")

    def test_a_newer_sar_packet_is_still_accepted_after_an_old_scout_packet(self):
        self.post(packet(2, ts=1500, battery=10))                      # rejected (replay)
        r = self.post(packet(3, ts=2001, name="SAR-001", battery=48, mode="MANUAL"))
        self.assertFalse(r.json()["stale"])
        self.assertEqual(self.row(SAR)["battery"], 48)

    def test_one_vehicles_timestamp_does_not_block_another(self):
        """SAR's clock runs far ahead of Scout's; Scout's own progression still applies."""
        self.post(packet(3, ts=99999, name="SAR-001", battery=40, mode="MANUAL"))
        r = self.post(packet(2, ts=2001, name="Scout", battery=77, mode="AUTO"))
        self.assertFalse(r.json()["stale"], "SAR's newer clock must not reject Scout's packet")
        self.assertEqual(self.row(SCOUT)["battery"], 77)

    def test_rejected_packet_still_proves_the_link_is_alive_for_that_vehicle_only(self):
        self.age(SCOUT, 20)
        self.post(packet(2, ts=1500, battery=10))          # replayed, but it DID arrive
        self.assertEqual(self.row(SCOUT)["comm_state"], "CONNECTED")
        self.assertEqual(self.row(SCOUT)["battery"], 79, "arrival ≠ accepting stale content")

    def test_the_guard_is_a_high_water_mark_that_recovers_on_its_own(self):
        """The mechanism behind an 'accepted=false for a while' episode: the mark only moves
        forward, so a vehicle whose own clock went backwards is rejected until its timestamps
        pass the mark again — a bounded window that ends without intervention."""
        verdicts = []
        for ts in (2001, 2002, 1997, 1998, 1999, 2003, 2004):    # a ~5 s backward clock step
            verdicts.append("A" if not self.post(packet(2, ts=ts, battery=79)).json()["stale"]
                            else "R")
        self.assertEqual("".join(verdicts), "AARRRAA")
        self.assertEqual(self.row(SAR)["battery"], 50, "SAR was unaffected throughout")

    def test_a_backend_restart_alone_cannot_cause_a_rejection_streak(self):
        """Explicitly pinned because it is the obvious suspect and it is NOT the cause: the
        guard needs a previously accepted timestamp, and a restart has none — so the first
        packet after a restart is always accepted and re-seeds the mark, however old it is."""
        self.post(packet(2, ts=9000, battery=79))
        main.latest_msg_ts_by_id.clear()                  # ← what a backend restart looks like
        main.current_vehicle_state.clear()
        r = self.post(packet(2, ts=10, battery=42))       # a far older agent timestamp
        self.assertFalse(r.json()["stale"], "no prior timestamp means nothing to compare against")
        self.assertEqual(self.row(SCOUT)["battery"], 42)

    def test_a_future_dated_packet_raises_the_mark_for_that_vehicle_only(self):
        """Documents a real fragility found while diagnosing the SAR episode: one outlier
        timestamp poisons that vehicle's mark until real time catches up. Confined to the one
        vehicle — Scout keeps being accepted normally."""
        self.post(packet(3, ts=99999, battery=50))
        self.assertTrue(self.post(packet(3, ts=2001, battery=49)).json()["stale"])
        self.assertFalse(self.post(packet(2, ts=2001, battery=78)).json()["stale"])
        self.assertEqual(self.row(SCOUT)["battery"], 78)


class PerVehicleLastKnown(MultiUsvBase):
    """Requirements 10–12: last-known values are per USV and never cross vehicles."""

    def setUp(self):
        super().setUp()
        self.post(packet(2, ts=3000, name="Scout", battery=79, mode="AUTO", heading=90))
        self.post(packet(3, ts=3000, name="SAR-001", battery=50, mode="MANUAL", heading=180))

    def test_missing_scout_battery_retains_only_scouts_last_known(self):
        self.post(packet(2, ts=3001, name="Scout", mode="AUTO", heading=91))   # battery absent
        self.assertEqual(self.row(SCOUT)["battery"], 79)
        self.assertEqual(self.row(SAR)["battery"], 50)

    def test_missing_sar_battery_retains_only_sars_last_known(self):
        self.post(packet(3, ts=3001, name="SAR-001", mode="MANUAL", heading=181))
        self.assertEqual(self.row(SAR)["battery"], 50)
        self.assertEqual(self.row(SCOUT)["battery"], 79)

    def test_last_known_stores_are_separate_dicts(self):
        self.assertEqual(main.last_known_telemetry[SCOUT]["heading"], 90)
        self.assertEqual(main.last_known_telemetry[SAR]["heading"], 180)

    def test_a_telemetry_less_packet_from_one_vehicle_blanks_nothing_on_the_other(self):
        self.post(packet(2, ts=3001, name="Scout", telemetry=False))
        sar = self.row(SAR)
        self.assertEqual((sar["battery"], sar["heading"], sar["telemetry"]["mode"]),
                         (50, 180, "MANUAL"))


class CanonicalIdentity(MultiUsvBase):
    """Requirements 13, 14 + the identity-collision half of the bug: every spelling of one
    vehicle's identity resolves to ONE record, and a display name is never an identity."""

    SPELLINGS = [3, "3", "usv-3", "USV-3", "SAR-001", "sar_001"]

    def test_every_spelling_resolves_to_the_same_canonical_record(self):
        for i, spelling in enumerate(self.SPELLINGS):
            self.post(packet(spelling, ts=4000 + i, name="SAR-001", battery=50 - i, mode="MANUAL"))
            self.assertEqual(main.canonical_id(spelling), SAR, f"{spelling!r} must map to usv-3")
        self.assertEqual(len([v for v in self.fleet() if v["id"] == SAR]), 1)
        self.assertEqual(self.row(SAR)["battery"], 50 - (len(self.SPELLINGS) - 1))

    def test_a_callsign_packet_never_lands_on_another_vehicle(self):
        """The old resolver did int("SAR-001".replace("usv-","")) and fell back to id 2 —
        SAR's telemetry, name and health silently overwrote SCOUT's record."""
        self.post(packet(2, ts=4000, name="Scout", battery=79, mode="AUTO", lat=56.7, lng=13.0))
        self.post(packet("SAR-001", ts=4001, name="SAR-001", battery=50, mode="MANUAL",
                         lat=56.71, lng=13.01))
        scout = self.row(SCOUT)
        self.assertEqual((scout["name"], scout["battery"], scout["telemetry"]["mode"]),
                         ("Scout", 79, "AUTO"))
        self.assertEqual(self.row(SAR)["battery"], 50)

    def test_no_duplicate_usv3_and_sar001_rows(self):
        self.post(packet(3, ts=4000, name="SAR-001", battery=50, mode="MANUAL"))
        ids = [v["id"] for v in self.fleet()]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate vehicle rows: {ids}")
        names = [v["name"] for v in self.fleet()]
        self.assertNotIn("USV-3", names, "the static USV-3 placeholder must not coexist with SAR-001")

    def test_scout_remains_scout_and_sar_remains_sar_across_many_packets(self):
        for i in range(5):
            self.post(packet(2, ts=5000 + i, name="Scout", battery=79))
            self.post(packet(3, ts=5000 + i, name="SAR-001", battery=50))
            self.assertEqual(self.row(SCOUT)["name"], "Scout")
            self.assertEqual(self.row(SAR)["name"], "SAR-001")

    def test_one_vehicle_cannot_rename_another(self):
        self.post(packet(3, ts=4000, name="RENAMED-BY-SAR", battery=50))
        self.assertEqual(self.row(SCOUT)["name"], "Scout")
        self.assertEqual(self.row(SAR)["name"], "RENAMED-BY-SAR")

    def test_display_name_is_not_an_identity_key(self):
        """Renaming a vehicle updates its record — it must not create a second one."""
        self.post(packet(3, ts=4000, name="SAR-001", battery=50))
        self.post(packet(3, ts=4001, name="RESCUE-7", battery=49))
        self.assertEqual(len([v for v in self.fleet() if v["id"] == SAR]), 1)
        self.assertEqual(self.row(SAR)["battery"], 49)

    def test_canonical_slug_is_published_and_stable(self):
        self.post(packet("USV-3", ts=4000, name="SAR-001", battery=50))
        self.assertEqual(self.row(SAR)["vehicle_id"], "usv-3")
        self.assertEqual(self.row(SCOUT)["vehicle_id"], "usv-2")

    def test_a_packet_with_no_resolvable_identity_is_rejected_not_merged(self):
        r = self.client.post("/agent/status", json={"message_type": "status",
                                                    "payload": {"comm_state": "CONNECTED",
                                                                "telemetry": {"battery": 5}}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])
        self.assertIsNone(self.row(SCOUT)["battery"], "an unidentified packet must touch nobody")


class ThirdVehicleNeedsNoCodeChange(MultiUsvBase):
    """Requirement 15: a third/fourth USV joins through the same code path — no branches."""

    def test_an_unconfigured_vehicle_is_discovered_from_its_packets(self):
        self.post(packet(2, ts=6000, name="Scout", battery=79, mode="AUTO"))
        self.post(packet(3, ts=6000, name="SAR-001", battery=50, mode="MANUAL"))
        self.post(packet(THIRD, ts=6000, name="Probe-4", battery=33, mode="GUIDED"))
        ids = [v["id"] for v in self.fleet()]
        self.assertIn(THIRD, ids)
        self.assertEqual(len(ids), len(set(ids)))
        third = self.row(THIRD)
        self.assertEqual((third["name"], third["battery"], third["comm_state"]),
                         ("Probe-4", 33, "CONNECTED"))
        self.assertEqual(third["vehicle_id"], "usv-4")
        # …and it did not disturb the two vehicles that were already there.
        self.assertEqual(self.row(SCOUT)["battery"], 79)
        self.assertEqual(self.row(SAR)["battery"], 50)

    def test_a_vehicle_with_a_non_numeric_identity_is_still_a_first_class_member(self):
        self.post(packet("probe-alpha", ts=6000, name="Probe Alpha", battery=61, mode="AUTO"))
        row = self.row("probe-alpha")
        self.assertEqual((row["name"], row["battery"], row["vehicle_id"]),
                         ("Probe Alpha", 61, "probe-alpha"))

    def test_a_configured_registry_adds_a_vehicle_with_no_code_change(self):
        import vehicle_registry
        reg = vehicle_registry.VehicleRegistry({
            "usv-2": {"id": 2, "display_name": "Scout", "aliases": ["Scout"]},
            "usv-3": {"id": 3, "display_name": "SAR-001", "aliases": ["SAR-001"]},
            "usv-9": {"id": 9, "display_name": "Guardian", "aliases": ["GUARDIAN-9"]},
        })
        self.assertEqual(reg.canonical_id("GUARDIAN-9"), 9)
        self.assertEqual(reg.default_display_name(9), "Guardian")
        self.assertEqual(reg.slug(9), "usv-9")
        self.assertEqual(reg.configured_ids(), [2, 3, 9])

    def test_registry_rejects_an_ambiguous_alias(self):
        import vehicle_registry
        with self.assertRaises(vehicle_registry.RegistryError):
            vehicle_registry.VehicleRegistry({
                "usv-2": {"id": 2, "aliases": ["Rescue"]},
                "usv-3": {"id": 3, "aliases": ["Rescue"]},
            })

    def test_the_shipped_registry_file_loads_and_names_the_live_fleet(self):
        """vehicles.json is the deployed configuration — a typo there must fail the build,
        not silently fall back to defaults and route a vehicle to the wrong record."""
        import vehicle_registry
        from pathlib import Path
        path = Path(main.__file__).resolve().parent / "vehicles.json"
        self.assertTrue(path.exists(), "vehicles.json ships with the station")
        reg = vehicle_registry.load_registry(path)
        self.assertEqual(reg.canonical_id("Scout"), SCOUT)
        self.assertEqual(reg.canonical_id("SAR-001"), SAR)
        self.assertEqual(reg.default_display_name(SAR), "SAR-001")
        self.assertEqual(reg.configured_ids(), main.REGISTRY.configured_ids())
        # Comment keys are documentation, never a vehicle.
        self.assertNotIn("_comment", [reg.slug(c) for c in reg.configured_ids()])


class NoCrossVehicleWrites(MultiUsvBase):
    """Requirement 17: mission, authority, agent status, health and position are per USV."""

    def setUp(self):
        super().setUp()
        self.post(packet(2, ts=7000, name="Scout", battery=79, mode="AUTO", lat=56.70, lng=13.00,
                         mission_state="EXECUTING", health={"gps_fix": 3, "cpu": 21},
                         agent={"current_behaviour": "surveying", "control_authority": "OPERATOR"}))
        self.post(packet(3, ts=7000, name="SAR-001", battery=50, mode="MANUAL", lat=56.71, lng=13.01,
                         mission_state="IDLE", health={"gps_fix": 2, "cpu": 88},
                         agent={"current_behaviour": "holding", "control_authority": "LOCAL_AGENT"}))

    def _snapshot(self, cid):
        v = self.row(cid)
        return {k: v[k] for k in ("mission", "mission_data", "agent_status", "health",
                                  "lat", "lng", "name", "battery", "telemetry")}

    def test_scout_packet_changes_nothing_on_sar(self):
        before = self._snapshot(SAR)
        self.post(packet(2, ts=7001, name="Scout", battery=60, mode="RTL", lat=55.0, lng=12.0,
                         mission_state="ABORTED", health={"gps_fix": 0, "cpu": 99},
                         agent={"current_behaviour": "returning", "control_authority": "LOCAL_AGENT"}))
        self.assertEqual(before, self._snapshot(SAR))

    def test_sar_packet_changes_nothing_on_scout(self):
        before = self._snapshot(SCOUT)
        self.post(packet(3, ts=7001, name="SAR-001", battery=20, mode="HOLD", lat=57.0, lng=14.0,
                         mission_state="EXECUTING", health={"gps_fix": 0, "cpu": 5},
                         agent={"current_behaviour": "searching", "control_authority": "OPERATOR"}))
        self.assertEqual(before, self._snapshot(SCOUT))

    def test_agent_reasoning_is_per_vehicle(self):
        self.assertEqual(self.row(SCOUT)["agent_status"]["current_behaviour"], "surveying")
        self.assertEqual(self.row(SAR)["agent_status"]["current_behaviour"], "holding")

    def test_health_is_per_vehicle(self):
        self.assertEqual(self.row(SCOUT)["health"]["cpu"], 21)
        self.assertEqual(self.row(SAR)["health"]["cpu"], 88)

    def test_commands_route_to_the_addressed_vehicle_only(self):
        r = self.client.post("/api/commands", json={"vehicle_id": "usv-3", "type": "SET_MODE_LOITER"})
        self.assertEqual(r.status_code, 200, r.text)
        scout_q = self.client.get("/api/commands/usv-2").json()["commands"]
        sar_q = self.client.get("/api/commands/usv-3").json()["commands"]
        self.assertEqual(scout_q, [])
        self.assertEqual(len(sar_q), 1)
        self.assertEqual(sar_q[0]["vehicle_id"], SAR)


class FleetResponseShape(MultiUsvBase):
    """Requirement 18 + the endpoint contract: a stable list of independently
    normalized records, every field present for every vehicle, every time."""

    REQUIRED = ("id", "vehicle_id", "name", "comm_state", "last_seen_age_s", "telemetry",
                "battery", "lat", "lng", "heading", "speed", "health", "mission",
                "mission_data", "agent_status", "home", "mavlink", "online")

    def test_every_row_carries_the_full_contract(self):
        self.post(packet(2, ts=8000, name="Scout", battery=79, mode="AUTO", lat=56.7, lng=13.0,
                         heading=90, groundspeed=1.2, mission_state="EXECUTING"))
        self.post(packet(3, ts=8000, name="SAR-001", battery=50, mode="MANUAL"))
        for row in self.fleet():
            for field in self.REQUIRED:
                self.assertIn(field, row, f"vehicle {row['id']} is missing {field}")

    def test_configured_vehicles_exist_before_first_contact(self):
        ids = [v["id"] for v in self.fleet()]
        self.assertEqual(ids, main.REGISTRY.configured_ids())
        for row in self.fleet():
            self.assertEqual(row["comm_state"], "UNKNOWN")
            self.assertFalse(row["contacted"])

    def test_ordering_is_stable_across_polls_and_arrival_order(self):
        first = [v["id"] for v in self.fleet()]
        self.post(packet(3, ts=8000, name="SAR-001", battery=50))     # SAR reports first…
        self.post(packet(2, ts=8000, name="Scout", battery=79))       # …Scout second
        self.assertEqual([v["id"] for v in self.fleet()], first)
        self.post(packet(2, ts=8001, name="Scout", battery=78))
        self.assertEqual([v["id"] for v in self.fleet()], first)

    def test_no_vehicle_disappears_because_another_reported(self):
        expected = len(main.REGISTRY.configured_ids())
        for i in range(6):
            self.post(packet(2 if i % 2 == 0 else 3, ts=9000 + i, battery=50))
            self.assertEqual(len(self.fleet()), expected)

    def test_per_vehicle_records_are_exposed_on_the_agent_status_endpoint(self):
        self.post(packet(2, ts=8000, name="Scout", battery=79))
        self.post(packet(3, ts=8000, name="SAR-001", battery=50))
        vehicles = self.client.get("/agent/status").json()["vehicles"]
        self.assertEqual(set(vehicles), {"usv-2", "usv-3"})
        self.assertEqual(vehicles["usv-2"]["name"], "Scout")
        self.assertEqual(vehicles["usv-3"]["name"], "SAR-001")


if __name__ == "__main__":
    unittest.main()
