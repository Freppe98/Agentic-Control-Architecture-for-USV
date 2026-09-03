import time


class MissionState:
    IDLE = "IDLE"
    WAITING = "WAITING"
    TRANSIT = "TRANSIT"
    SEARCH = "SEARCH"
    RETURN = "RETURN"
    ERROR = "ERROR"
    OVERRIDE = "OVERRIDE"


class MissionRunner:
    """
    Local Agent's own record of mission phase.

    The vehicle (Flask/Pixhawk) owns mission identity (current_mission_id,
    mission_active) and raw progress (current_waypoint, mission_count) --
    set via /start_mission and executed onboard regardless of comm state.
    This class owns the interpretation of that progress into a mission
    phase (TRANSIT/SEARCH/RETURN).

    Continuity: a full report failure (`error` in the vehicle's mission
    status -- Flask or the MAVLink bridge is down) moves to ERROR rather
    than silently reporting IDLE and losing that we were mid-mission. A
    *partial* read (heartbeat or mission item momentarily missing, but no
    top-level error) holds the last known in-progress phase instead of
    collapsing to TRANSIT, so one flaky poll doesn't misreport an ongoing
    SEARCH as "just starting".

    Known limitation: TRANSIT/SEARCH/RETURN is inferred purely from
    waypoint position in the uploaded mission (first item = TRANSIT, last
    item = RETURN, everything between = SEARCH). This assumes a single
    uploaded mission whose last waypoint is the return-to-home point. It
    does not hold if the return leg has more than one waypoint (only the
    final point reads as RETURN; earlier return-path points still read as
    SEARCH) or if a mission has no return leg at all (the last search
    point misreports as RETURN). The vehicle currently exposes no signal
    distinguishing a "search" waypoint from a "return" waypoint, so this
    can't be fixed on the Local Agent side alone.
    """

    def __init__(self):
        self.mission_id = None
        self.state = MissionState.IDLE
        self.started_at = None
        self.last_updated = None

    def update(self, vehicle_mission_status: dict) -> str:
        if not vehicle_mission_status or "error" in vehicle_mission_status:
            self.state = MissionState.ERROR if self.mission_id else MissionState.IDLE
            return self.state

        mission_id = vehicle_mission_status.get("current_mission_id")
        active = vehicle_mission_status.get("mission_active")
        waypoint = vehicle_mission_status.get("current_waypoint")
        count = vehicle_mission_status.get("mission_count")

        if mission_id and mission_id != self.mission_id:
            self.mission_id = mission_id
            self.started_at = time.time()

        in_progress = self.state in (MissionState.TRANSIT, MissionState.SEARCH, MissionState.RETURN)

        if not active:
            self.state = MissionState.WAITING if self.mission_id else MissionState.IDLE
        elif waypoint is None or count is None:
            if not in_progress:
                self.state = MissionState.TRANSIT
            # else: hold the last known in-progress phase through this gap
        elif count <= 1 or waypoint <= 0:
            self.state = MissionState.TRANSIT
        elif waypoint >= count - 1:
            self.state = MissionState.RETURN
        else:
            self.state = MissionState.SEARCH

        self.last_updated = time.time()
        return self.state

    def to_dict(self) -> dict:
        # current_mission_id already passes through from the vehicle's raw
        # mission dict -- don't duplicate identity under a second key here.
        return {
            "mission_state": self.state,
            "started_at": self.started_at,
        }
