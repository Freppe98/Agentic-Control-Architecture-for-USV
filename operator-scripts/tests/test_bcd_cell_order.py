"""BCD cell-ORDER and cell-ORIENTATION optimisation (planning._bcd_cell_plan).

Run from operator-scripts/:  python -m unittest tests.test_bcd_cell_order

THE REGRESSION THIS FILE PINS
-----------------------------
`_bcd_cells` decides WHAT the cells are; until this existed, the order they were covered in was
their geometric sort key (lowest sweep row, then lowest U) and each cell's sweep direction was
picked one cell at a time from local terms. On the operator's large planning polygon that read
as: the east region, then the column south of the obstacle, then the column north of it, then
the west region — hand-overs of 119.8 m + 29.0 m + 78.3 m, entered by a 196.7 m survey-entry
connector from an approach that ended beside a completely different cell. Every one of those
legs was individually the shortest safe path available. The ORDER was the defect.

The order and the per-cell orientation are now chosen together, by measuring candidate
hand-overs with the same transition policy the route is drawn with (F2 → F4 → aligned → A*) and
minimising the total, with the survey entry and the return connector as boundary conditions.

WHAT IS ASSERTED
----------------
  * COVERAGE IS AN INPUT. The emitted fragments are exactly the clipped lane fragments, with
    byte-identical coordinates and byte-identical cell membership, whatever order they are flown
    in — moving the entry anchor changes the sequence and not one byte of the coverage.
  * The sequence is TOPOLOGY-AWARE: hand-overs follow the BCD adjacency graph, except for the
    single crossing an obstacle genuinely forces.
  * The search is EXACT: on every geometry small enough to enumerate, it returns the same cost
    as brute force over every order × orientation, and it beats nearest-next greedy.
  * BOUNDARY CONDITIONS BITE: where the vehicle enters the survey decides the first cell, and
    where it must leave for decides the last.
  * The contracts underneath are untouched: geometry validation green, the F2/F4 hierarchy
    unchanged, no new use of the A* fallback, and the result deterministic.
"""

import copy
import itertools
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402
from tests.test_bcd_coverage import (BOX, CONCAVE_L, IRREGULAR, _m2ll,  # noqa: E402
                                     _rect, _rot_rect)

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

SHORE_M = 5.0
NO_GO_M = 5.0
SPACING_M = 10.0

# A no-go tall in the survey frame splits each crossed lane into a piece each side of it, so the
# decomposition gets two cells occupying the SAME sweep rows; one wide in the survey frame breaks
# the sweep into runs of rows instead. Both shapes are exercised, at 0° and rotated, because the
# sequence that is coherent for one is not the sequence that is coherent for the other.
CENTRE_ZONE = _rect(90, 20, 115, 100)
WIDE_ZONE = _rect(30, 52, 170, 68)
TALL_ZONE = _rect(92, 15, 108, 105)
TWO_ZONES = [_rect(50, 30, 75, 90), _rect(130, 30, 155, 90)]
IRREGULAR_ZONES = [_rot_rect(60, 45, 105, 90, 25.0), _rect(140, 60, 170, 95)]

# Anchors well outside the survey, so entering from one is unambiguously different from the other.
SW_ANCHOR = _m2ll(-30, -30)
NE_ANCHOR = _m2ll(230, 150)


def _inputs(zones=(), angle=0.0, boundary=None, home=None, approach=None, returns=None):
    inp = {"boundary": list(boundary or BOX), "no_go_zones": [list(z) for z in zones],
           "shoreline_clearance_m": SHORE_M, "no_go_clearance_m": NO_GO_M,
           "lane_spacing_m": SPACING_M, "primary_angle_deg": angle}
    if home is not None:
        inp["home"] = list(home)
    if approach is not None:
        inp["approach_waypoints"] = [list(p) for p in approach]
    if returns is not None:
        inp["return_waypoints"] = [list(p) for p in returns]
    return inp


# The required matrix: no obstacle, a central no-go, a split across the sweep, a split along it,
# a rotated survey angle, a concave boundary, two no-go zones, and an irregular polygon with
# several cells.
MATRIX = {
    "rectangle, no obstacle": _inputs(),
    "central no-go": _inputs([CENTRE_ZONE]),
    "split across the sweep (wide zone)": _inputs([WIDE_ZONE]),
    "split along the sweep (tall zone)": _inputs([TALL_ZONE]),
    "rotated survey angle": _inputs([CENTRE_ZONE], angle=35.0),
    "rotated survey angle, wide zone": _inputs([WIDE_ZONE], angle=70.0),
    "concave boundary": _inputs(boundary=CONCAVE_L),
    "two no-go zones": _inputs(TWO_ZONES),
    "irregular polygon, two zones": _inputs(IRREGULAR_ZONES, boundary=IRREGULAR),
}
SPLIT_CASES = [n for n in MATRIX if MATRIX[n]["no_go_zones"]]


def _gen(inp):
    return planning.generate_survey(copy.deepcopy(inp), max_route_waypoints=5000)


def _plan_of(pkg, pass_kind="primary"):
    for p in pkg["route_quality"]["coverage_cell_plans"]:
        if p["pass_kind"] == pass_kind:
            return p
    raise AssertionError("no cell plan reported for the %s pass" % pass_kind)


def _geometry_of(inp):
    """The decomposition the ordering is handed: (grid, frame, fragments, cells)."""
    n = planning.normalize_generate_inputs(copy.deepcopy(inp))
    grid = planning._NavGrid(n["boundary"], n["shoreline_clearance_m"], n["no_go_zones"],
                             step_m=n["lane_spacing_m"],
                             no_go_clearance=n["no_go_clearance_m"])
    frame = planning._SurveyFrame(grid, n["primary_angle_deg"])
    fragments, _skipped = planning._lane_fragments(frame, n["lane_spacing_m"])
    return n, grid, frame, fragments, planning._bcd_cells(fragments)


def _cost_model_of(inp):
    """The cost model `_bcd_cell_plan` minimises, with this plan's own boundary anchors."""
    n, _grid, frame, _frags, cells = _geometry_of(inp)
    entry = (n["approach_waypoints"][-1] if n["approach_waypoints"] else n["home"])
    exit_ = (n["return_waypoints"][0] if n["return_waypoints"] else n["home"])
    model = planning._bcd_cost_model(frame, cells, n["lane_spacing_m"], entry, exit_)
    return frame, cells, model


def _sequence_cost(model, n, order, combo):
    total = model["enter"](order[0], combo[0])
    for k in range(n - 1):
        total += model["step"](order[k], combo[k], order[k + 1], combo[k + 1])
    return total + model["leave"](order[-1], combo[-1])


def _chosen_sequence(frame, cells, model, inp):
    """The order + orientation `_bcd_cell_plan` picks, as indices into `model["states"]`."""
    norm = planning.normalize_generate_inputs(copy.deepcopy(inp))
    entry = (norm["approach_waypoints"][-1] if norm["approach_waypoints"] else norm["home"])
    exit_ = (norm["return_waypoints"][0] if norm["return_waypoints"] else norm["home"])
    _plan, diag = planning._bcd_cell_plan(frame, cells, norm["lane_spacing_m"], entry, exit_)
    order = diag["cell_order"]
    combo = []
    for pos, chosen in enumerate(diag["cell_orientations"]):
        cell_id = order[pos]
        combo.append(next(k for k, st in enumerate(model["states"][cell_id])
                          if st["ascending"] == chosen["ascending"]
                          and st["first_row_forward"] == chosen["first_row_forward"]))
    return order, combo, diag


def _greedy_sequence(model, n):
    """Nearest-next over cells AND orientations — the obvious heuristic the exact search has to
    beat. Deterministic: ties resolve to the lowest (cell, orientation)."""
    start = min(((round(model["enter"](i, si), 6), i, si)
                 for i in range(n) for si in range(4)))
    order, combo = [start[1]], [start[2]]
    remaining = set(range(n)) - {start[1]}
    while remaining:
        i, si = order[-1], combo[-1]
        best = min((round(model["step"](i, si, j, sj), 6), j, sj)
                   for j in remaining for sj in range(4))
        order.append(best[1])
        combo.append(best[2])
        remaining.discard(best[1])
    return order, combo


# The operator's own large planning polygon at 150 degrees — the geometry the F5 investigation
# closed on, and the one whose in-cell ladder varies most among real plans. `planning_drafts/` is
# operator-local and gitignored, so the inputs are pinned here rather than loaded from a draft.
OPERATOR_150 = {
    "boundary": [[12.88096010684967, 56.66305453967672], [12.879302501678467, 56.662780234938246],
                 [12.878948450088501, 56.66207529209771], [12.879930138587953, 56.66154747608107],
                 [12.88127660751343, 56.66138524660315], [12.882682085037233, 56.661730352109856],
                 [12.882977128028871, 56.662391058110316], [12.882553339004518, 56.662883630494164],
                 [12.88096010684967, 56.66305453967672]],
    "no_go_zones": [[[12.880809903144838, 56.66258277866563], [12.880611419677734, 56.66207250487742],
                     [12.881206870079042, 56.662013513242556], [12.881351709365845, 56.66252968691718],
                     [12.880809903144838, 56.66258277866563]]],
    "home": [12.881426811218263, 56.663529568953855],
    "approach_waypoints": [[12.881094217300415, 56.66352367002259], [12.880579233169557, 56.66335555009301],
                           [12.880209088325502, 56.66312843952367], [12.879731655120851, 56.66294851978939]],
    "return_waypoints": [[12.879731655120851, 56.66294851978939], [12.880209088325502, 56.66312843952367],
                         [12.880579233169557, 56.66335555009301], [12.881094217300415, 56.66352367002259]],
    "shoreline_clearance_m": 1.0, "no_go_clearance_m": 5.0, "lane_spacing_m": 10.0,
    "primary_angle_deg": 150.0, "dual_pass": False, "secondary_angle_deg": None,
}


def _ladders(cell):
    """One cell's in-cell lane-turn ladder under each of its four traversals."""
    return [s["internal_transit_m"] for s in planning._cell_traversal_states(cell)]


def _plan_indices(states, diag):
    """`diag`'s chosen plan as (order, combo) indices into `states`."""
    order = diag["cell_order"]
    combo = [next(k for k, st in enumerate(states[order[p]])
                  if st["ascending"] == diag["cell_orientations"][p]["ascending"]
                  and st["first_row_forward"] == diag["cell_orientations"][p]["first_row_forward"])
             for p in range(len(order))]
    return order, combo


def _ladder_regret_m(inp):
    """Metres the plan chosen WITHOUT the in-cell ladder loses, once the ladder IS counted.

    Folds each cell's ladder into the boundary/step terms (it depends on the state, not on the
    order) and re-runs the same exact Held-Karp, then compares the two plans on the SAME
    ladder-aware total. Zero means the current objective already picked the best total route.
    Costs are compared, never plan identity, so a tie cannot read as a change."""
    frame, cells, model = _cost_model_of(inp)
    n = len(cells)
    states = model["states"]
    ladder = {(i, si): states[i][si]["internal_transit_m"] for i in range(n) for si in range(4)}
    norm = planning.normalize_generate_inputs(copy.deepcopy(inp))
    entry = (norm["approach_waypoints"][-1] if norm["approach_waypoints"] else norm["home"])
    exit_ = (norm["return_waypoints"][0] if norm["return_waypoints"] else norm["home"])
    _plan, diag = planning._bcd_cell_plan(frame, cells, norm["lane_spacing_m"], entry, exit_)
    order, combo = _plan_indices(states, diag)
    aware_order, aware_combo, _total = planning._held_karp(
        n,
        lambda i, si, j, sj: model["step"](i, si, j, sj) + ladder[(j, sj)],
        lambda i, si: model["enter"](i, si) + ladder[(i, si)],
        model["leave"])

    def total(o, c):
        return _sequence_cost(model, n, o, c) + sum(ladder[(o[k], c[k])] for k in range(n))
    return total(order, combo) - total(aware_order, aware_combo)


def _emitted_fragment_keys(pkg):
    """Every emitted coverage fragment as (cell id, unordered endpoint pair)."""
    return [(f["cell_index"], tuple(sorted([tuple(f["start"]), tuple(f["end"])])))
            for f in pkg["route_quality"]["coverage_fragments"]]


def _clipped_fragment_keys(frame, cells):
    """The same, straight from the decomposition — what the route is REQUIRED to emit."""
    out = []
    for cell_id, cell in enumerate(cells):
        for frag in cell:
            a = [round(c, 7) for c in frame.rot_to_deg(frag["rot"][0])]
            b = [round(c, 7) for c in frame.rot_to_deg(frag["rot"][1])]
            out.append((cell_id, tuple(sorted([tuple(a), tuple(b)]))))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTopologyGraph(unittest.TestCase):
    """The adjacency graph the ordering is constrained by — a real topology, not a clique."""

    def test_a_region_with_nothing_splitting_a_lane_is_one_cell_with_no_neighbours(self):
        for name in ("rectangle, no obstacle", "concave boundary"):
            with self.subTest(name):
                _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
                self.assertEqual(len(cells), 1)
                self.assertEqual(planning._bcd_adjacency(cells), [frozenset()])
                plan = _plan_of(_gen(MATRIX[name]))
                self.assertEqual(plan["cell_order"], [0])
                self.assertEqual(plan["handovers"], [])
                self.assertEqual(plan["inter_cell_transit_m"], 0.0)

    def test_the_graph_is_the_split_merge_relation_and_never_a_clique(self):
        for name in SPLIT_CASES:
            with self.subTest(name):
                _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
                adjacency = planning._bcd_adjacency(cells)
                self.assertGreater(len(cells), 1, "an exclusion must decompose the sweep")
                for i, neighbours in enumerate(adjacency):
                    self.assertNotIn(i, neighbours, "a cell is not its own neighbour")
                    for j in neighbours:                       # symmetric
                        self.assertIn(i, adjacency[j])
                    # Every edge really is two fragments meeting across one sweep row.
                    for j in neighbours:
                        self.assertTrue(
                            any(abs(f["row"] - g["row"]) == 1
                                and planning._fragments_overlap_in_u(f, g)
                                for f in cells[i] for g in cells[j]),
                            f"cells {i} and {j} are called adjacent but share no sweep boundary")
                self.assertLess(sum(len(a) for a in adjacency), len(cells) * (len(cells) - 1),
                                "the topology graph is complete — it says nothing")

    def test_two_columns_beside_one_obstacle_are_not_adjacent_to_each_other(self):
        # The whole point of the graph: the two cells either side of an exclusion occupy the same
        # sweep rows, so the decomposition does NOT connect them — the obstacle is between them.
        _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX["central no-go"])
        adjacency = planning._bcd_adjacency(cells)
        rows = [(min(f["row"] for f in c), max(f["row"] for f in c)) for c in cells]
        siblings = [(i, j) for i in range(len(cells)) for j in range(i + 1, len(cells))
                    if rows[i] == rows[j]]
        self.assertTrue(siblings, "this fixture must produce two cells over the same rows")
        for i, j in siblings:
            self.assertNotIn(j, adjacency[i])


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTraversalStates(unittest.TestCase):
    """Four legal traversals per cell — the same coverage, entered at each of its four corners."""

    def test_every_cell_offers_four_traversals_that_pair_into_reverses(self):
        for name in MATRIX:
            with self.subTest(name):
                _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
                for cell in cells:
                    states = planning._cell_traversal_states(cell)
                    self.assertEqual(len(states), 4)
                    self.assertEqual(
                        sorted((s["ascending"], s["first_row_forward"]) for s in states),
                        [(False, False), (False, True), (True, False), (True, True)])
                    # FORWARD/REVERSE: each state's exact reverse is another of the four.
                    def signature(state):
                        return [(f["entry_rot"], f["exit_rot"]) for f in state["fragments"]]

                    def reversed_signature(state):
                        return [(f["exit_rot"], f["entry_rot"])
                                for f in reversed(state["fragments"])]
                    sigs = [signature(s) for s in states]
                    for s in states:
                        self.assertIn(reversed_signature(s), sigs,
                                      "a traversal has no legal reverse among the four")

    def test_every_traversal_covers_the_same_fragments_and_the_same_survey_line(self):
        for name in MATRIX:
            with self.subTest(name):
                _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
                for cell in cells:
                    states = planning._cell_traversal_states(cell)
                    members = [sorted(sorted(f["rot"]) for f in s["fragments"]) for s in states]
                    for m in members[1:]:
                        self.assertEqual(m, members[0],
                                         "an orientation changed WHICH fragments are covered")
                    for s in states[1:]:
                        self.assertAlmostEqual(s["coverage_m"], states[0]["coverage_m"], places=9,
                                               msg="an orientation changed the survey line length")

    def test_each_traversal_is_still_lane_monotonic_and_alternating(self):
        for name in SPLIT_CASES:
            with self.subTest(name):
                _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
                for cell in cells:
                    for state in planning._cell_traversal_states(cell):
                        rows = [f["row"] for f in state["fragments"]]
                        self.assertEqual(rows, sorted(rows, reverse=not state["ascending"]),
                                         "coverage does not advance monotonically through the "
                                         "sweep inside the cell")
                        # The along-U direction flips exactly once per produced lane.
                        seen, expect = None, state["first_row_forward"]
                        for f in state["fragments"]:
                            if f["row"] != seen:
                                seen = f["row"]
                                direction = expect
                                expect = not expect
                            self.assertEqual(f["dir"][0] > 0, direction,
                                             "the boustrophedon alternation was broken")

    def test_the_search_really_uses_more_than_one_orientation(self):
        used = set()
        for name in MATRIX:
            for chosen in _plan_of(_gen(MATRIX[name]))["cell_orientations"]:
                used.add((chosen["ascending"], chosen["first_row_forward"]))
        self.assertGreater(len(used), 1,
                           "every cell came out in the same orientation — the second bit is dead")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheInCellLadderIsReportedNotOptimised(unittest.TestCase):
    """The WITHIN-cell lane-turn ladder: what varies, what does not, and what the cost ignores.

    `_cell_traversal_states` once claimed the ladder was "the same in every state, so it cannot
    swing the objective". The first half of that is false. Coverage IS invariant across the four
    traversals; the repositioning BETWEEN lanes is not, because the alternation turns at one end
    of each lane pair or the other and a cell's two U-boundaries are rarely mirror images. These
    tests pin the real invariant (at most two values, one per reverse pair), pin that the cost
    function genuinely does not read it, and pin the evidence for leaving it out: on the operator
    geometry the plan chosen without it is already the best total route with it counted."""

    def test_the_lane_turn_ladder_is_not_invariant_across_the_four_traversals(self):
        """The false claim, inverted into an assertion: somewhere in the matrix it DOES vary."""
        varying = []
        for name in MATRIX:
            _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
            for ci, cell in enumerate(cells):
                vals = _ladders(cell)
                if max(vals) - min(vals) > 1e-6:
                    varying.append((name, ci, max(vals) - min(vals)))
        self.assertTrue(varying,
                        "no cell's in-cell ladder varied with orientation - if that is really "
                        "true the docs may go back to claiming invariance")
        self.assertGreater(max(v[2] for v in varying), 1.0,
                           "the variation is below a metre everywhere; the reported spread and "
                           "the docstring's numbers need revisiting")

    def test_the_ladder_takes_at_most_two_values_one_per_reverse_pair(self):
        """WHY it varies, stated exactly: a reverse pair turns at the same lane ends, so the two
        pairs give at most two distinct ladders - never three, never four."""
        for name in MATRIX:
            with self.subTest(name):
                _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX[name])
                for ci, cell in enumerate(cells):
                    states = planning._cell_traversal_states(cell)
                    distinct = {round(s["internal_transit_m"], 6) for s in states}
                    self.assertLessEqual(len(distinct), 2,
                                         "cell %d of %r offered %d distinct ladders; the reverse-"
                                         "pair structure is broken" % (ci, name, len(distinct)))

                    def signature(state):
                        return [(f["entry_rot"], f["exit_rot"]) for f in state["fragments"]]
                    sigs = [signature(s) for s in states]
                    for s in states:
                        mate = sigs.index([(f["exit_rot"], f["entry_rot"])
                                           for f in reversed(s["fragments"])])
                        self.assertAlmostEqual(
                            states[mate]["internal_transit_m"], s["internal_transit_m"], places=6,
                            msg="a traversal and its exact reverse flew different ladders")

    def test_a_mirror_symmetric_cell_is_where_all_four_agree(self):
        """The rectangle is the case the old claim generalised from: both ends of every lane pair
        line up, so the two complementary turn sets are equal. The exception, not the rule."""
        _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX["rectangle, no obstacle"])
        self.assertEqual(len(cells), 1)
        vals = _ladders(cells[0])
        self.assertAlmostEqual(max(vals) - min(vals), 0.0, places=9)
        _n, _grid, _frame, _frags, cells = _geometry_of(MATRIX["concave boundary"])
        self.assertGreater(max(_ladders(cells[0])) - min(_ladders(cells[0])), 1.0,
                           "a concave shore should break the mirror symmetry")

    def test_the_ladder_is_not_an_input_to_the_cost_model(self):
        """The exclusion is structural, not incidental: corrupting `internal_transit_m` beyond all
        recognition must leave the chosen order and orientations byte-identical."""
        real = planning._cell_traversal_states

        def poisoned(cell):
            states = real(cell)
            for k, st in enumerate(states):
                st["internal_transit_m"] = 1.0e6 * (k + 1)
            return states

        for name in ("central no-go", "irregular polygon, two zones"):
            with self.subTest(name):
                frame, cells, _model = _cost_model_of(MATRIX[name])
                norm = planning.normalize_generate_inputs(copy.deepcopy(MATRIX[name]))
                entry = (norm["approach_waypoints"][-1] if norm["approach_waypoints"]
                         else norm["home"])
                exit_ = (norm["return_waypoints"][0] if norm["return_waypoints"] else norm["home"])
                _p, clean = planning._bcd_cell_plan(frame, cells, norm["lane_spacing_m"],
                                                    entry, exit_)
                planning._cell_traversal_states = poisoned
                try:
                    _p, dirty = planning._bcd_cell_plan(frame, cells, norm["lane_spacing_m"],
                                                        entry, exit_)
                finally:
                    planning._cell_traversal_states = real
                self.assertEqual(dirty["cell_order"], clean["cell_order"])
                self.assertEqual(dirty["cell_orientations"], clean["cell_orientations"])
                self.assertEqual(dirty["handovers"], clean["handovers"])

    def test_the_diagnostics_report_the_chosen_ladder_and_the_omitted_spread(self):
        """The ladder is no longer invisible: the plan reports what it spent and how much the four
        traversals differed by, so a geometry where the omission grows shows up instead of hiding."""
        for name in MATRIX:
            with self.subTest(name):
                pkg = _gen(MATRIX[name])
                plan = _plan_of(pkg)
                _n, _g, _f, _fr, cells = _geometry_of(MATRIX[name])
                self.assertGreaterEqual(plan["in_cell_transit_m"], 0.0)
                self.assertAlmostEqual(
                    plan["in_cell_transit_spread_m"],
                    round(sum(max(_ladders(c)) - min(_ladders(c)) for c in cells), 2), places=2)
                # A straight-line view of a ladder the route really flies: it has to land near the
                # emitted one, and can never exceed the safe path drawn for it.
                rq = pkg["route_quality"]
                emitted = (rq["in_coverage_transition_length_m"]
                           - rq["inter_cell_transit_length_m"])
                self.assertLessEqual(plan["in_cell_transit_m"], emitted + 1e-6)
                self.assertGreater(plan["in_cell_transit_m"], 0.9 * emitted,
                                   "the reported ladder is nowhere near the emitted one")

    def test_counting_the_ladder_would_not_change_the_operator_plan(self):
        """THE EVIDENCE FOR LEAVING IT OUT. On the operator's own 150 degree geometry the plan
        chosen without the ladder is already optimal for total route WITH it counted, so folding
        it in would buy nothing while letting a cheap turn ladder outbid a hand-over."""
        self.assertAlmostEqual(_ladder_regret_m(OPERATOR_150), 0.0, places=6)

    def test_the_operator_plan_still_reports_the_accepted_150_degree_numbers(self):
        """The accepted F5 result, pinned: 4 cells, order 0-2-3-1, no fallback, no non-adjacent
        hand-over, and a real ladder spread the objective declines to spend."""
        plan = _plan_of(_gen(OPERATOR_150))
        self.assertEqual(plan["mode"], "exact-held-karp")
        self.assertEqual(plan["cell_count"], 4)
        self.assertEqual(plan["cell_order"], [0, 2, 3, 1])
        self.assertEqual(plan["non_adjacent_handover_count"], 0)
        self.assertAlmostEqual(plan["largest_inter_cell_transit_m"], 104.35, places=2)
        self.assertAlmostEqual(plan["entry_boundary_m"], 20.47, places=2)
        self.assertAlmostEqual(plan["return_boundary_m"], 30.50, places=2)
        self.assertGreater(plan["in_cell_transit_spread_m"], 1.0)


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheSearchIsExact(unittest.TestCase):
    """Globally optimal for the stated cost, and better than the obvious heuristic."""

    BRUTE_MAX_CELLS = 5

    def test_it_matches_brute_force_over_every_order_and_orientation(self):
        for name, inp in MATRIX.items():
            frame, cells, model = _cost_model_of(inp)
            n = len(cells)
            if n > self.BRUTE_MAX_CELLS:
                continue
            with self.subTest(name):
                order, combo, _diag = _chosen_sequence(frame, cells, model, inp)
                got = _sequence_cost(model, n, order, combo)
                best = min(_sequence_cost(model, n, list(o), list(c))
                           for o in itertools.permutations(range(n))
                           for c in itertools.product(range(4), repeat=n))
                self.assertAlmostEqual(got, best, places=6,
                                       msg=f"[{name}] the search missed the optimum")

    def test_it_is_never_worse_than_nearest_next_greedy(self):
        """10: the case greedy gets wrong. Greedy takes the cheapest next hand-over and pays for
        it later; the exact search trades a longer hand-over now for a much cheaper remainder."""
        strictly_better = 0
        for name, inp in MATRIX.items():
            frame, cells, model = _cost_model_of(inp)
            n = len(cells)
            if n < 2:
                continue
            with self.subTest(name):
                order, combo, _diag = _chosen_sequence(frame, cells, model, inp)
                exact = _sequence_cost(model, n, order, combo)
                greedy = _sequence_cost(model, n, *_greedy_sequence(model, n))
                self.assertLessEqual(exact, greedy + 1e-6,
                                     f"[{name}] greedy beat the exact search")
                if exact < greedy - 1e-6:
                    strictly_better += 1
        self.assertGreater(strictly_better, 0,
                           "no fixture distinguishes the exact search from greedy — the "
                           "regression matrix cannot show the optimiser is worth anything")

    def test_the_lower_bound_never_over_estimates(self):
        """The one property the bounded search depends on for its exactness."""
        for name, inp in MATRIX.items():
            frame, cells, model = _cost_model_of(inp)
            n = len(cells)
            with self.subTest(name):
                for i in range(n):
                    for j in range(n):
                        if i == j:
                            continue
                        for si in range(4):
                            for sj in range(4):
                                lb = model["step_lb"](i, si, j, sj)
                                true = model["step"](i, si, j, sj)
                                self.assertLessEqual(lb, true + 1e-6,
                                                     f"[{name}] the bound over-estimates")
                    for si in range(4):
                        self.assertLessEqual(model["enter_lb"](i, si),
                                             model["enter"](i, si) + 1e-6)
                        self.assertLessEqual(model["leave_lb"](i, si),
                                             model["leave"](i, si) + 1e-6)


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTopologyAwareness(unittest.TestCase):
    """The sequence follows the decomposition instead of jumping between unrelated cells."""

    def test_hand_overs_follow_the_adjacency_graph_bar_the_obstacle_crossing(self):
        for name in SPLIT_CASES:
            with self.subTest(name):
                inp = MATRIX[name]
                plan = _plan_of(_gen(inp))
                # A sweep split by an obstacle has to cross it once to reach the far column, and
                # that crossing is BETWEEN cells the decomposition does not connect. Everything
                # else must follow the topology.
                self.assertLessEqual(plan["non_adjacent_handover_count"],
                                     max(1, len(inp["no_go_zones"])),
                                     f"[{name}] the sequence jumps between unrelated cells")
                self.assertLess(plan["non_adjacent_handover_count"], plan["cell_count"] - 1,
                                f"[{name}] most hand-overs ignore the topology")

    def test_the_longest_hand_over_is_never_worse_than_the_geometric_order_s(self):
        """The visible defect was ONE enormous hand-over, not the total. Cutting the total while
        leaving a longer single jump behind would not be the fix, so the worst leg is pinned
        separately, against the order this replaced."""
        for name, inp in MATRIX.items():
            frame, cells, model = _cost_model_of(inp)
            n = len(cells)
            if n < 2:
                continue
            with self.subTest(name):
                order, combo, diag = _chosen_sequence(frame, cells, model, inp)
                worst = max(h["length_m"] for h in diag["handovers"])
                geometric = list(range(n))
                was = max(model["step_detail"](k, 0, k + 1, 0)[1] for k in range(n - 1))
                if worst > was + 1e-6:
                    # A longer worst leg is only acceptable if it BOUGHT something: the whole
                    # sequence must come out strictly cheaper for it. Trading a bigger single
                    # jump for nothing is exactly the defect being removed.
                    self.assertLess(_sequence_cost(model, n, order, combo),
                                    _sequence_cost(model, n, geometric, [0] * n) - 1e-6,
                                    f"[{name}] the longest single hand-over got worse and the "
                                    f"sequence is no cheaper for it")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestBoundaryConditions(unittest.TestCase):
    """Where the vehicle arrives from, and where it must leave for, are part of the ordering."""

    def test_the_entry_anchor_decides_which_cell_is_covered_first(self):
        sw = _plan_of(_gen(_inputs([CENTRE_ZONE], home=SW_ANCHOR)))
        ne = _plan_of(_gen(_inputs([CENTRE_ZONE], home=NE_ANCHOR)))
        self.assertNotEqual(sw["cell_order"][0], ne["cell_order"][0],
                            "entering the survey from opposite corners produced the same first "
                            "cell — the entry anchor is not reaching the ordering")

    def test_entering_from_a_corner_beats_entering_from_the_far_side(self):
        """The survey-entry connector is a real executed leg, so the first cell may not be chosen
        in ignorance of it: covering the same survey from the same corner must not be beaten, on
        that leg, by the sequence chosen for the opposite corner."""
        near = _gen(_inputs([CENTRE_ZONE], home=SW_ANCHOR))
        far = _gen(_inputs([CENTRE_ZONE], home=NE_ANCHOR))

        def entry_m(pkg):
            return next(s["length_m"] for s in pkg["segments"]
                        if s["kind"] == "survey_entry_connector")
        near_plan, far_plan = _plan_of(near), _plan_of(far)
        self.assertAlmostEqual(near_plan["entry_boundary_m"], entry_m(near), delta=0.05,
                               msg="the entry term is not the connector the route builds")
        self.assertAlmostEqual(far_plan["entry_boundary_m"], entry_m(far), delta=0.05,
                               msg="the entry term is not the connector the route builds")

    def test_the_return_anchor_decides_where_the_survey_finishes(self):
        """12: the return side is a BOUNDARY COST, not a free parameter. Holding the approach
        fixed and moving only the return chain must be able to change the sequence, and the term
        the search used must be the return connector the route really builds."""
        approach = [_m2ll(-20, -20)]
        back_home = _gen(_inputs([CENTRE_ZONE], home=SW_ANCHOR, approach=approach,
                                 returns=[_m2ll(-20, -20)]))
        far_side = _gen(_inputs([CENTRE_ZONE], home=SW_ANCHOR, approach=approach,
                                returns=[_m2ll(220, 140)]))
        a, b = _plan_of(back_home), _plan_of(far_side)
        self.assertNotEqual(
            (a["cell_order"], [(o["ascending"], o["first_row_forward"])
                               for o in a["cell_orientations"]]),
            (b["cell_order"], [(o["ascending"], o["first_row_forward"])
                               for o in b["cell_orientations"]]),
            "the return side never reaches the ordering")
        for pkg, plan in ((back_home, a), (far_side, b)):
            built = next(s["length_m"] for s in pkg["segments"]
                         if s["kind"] == "return_connector")
            self.assertAlmostEqual(plan["return_boundary_m"], built, delta=0.05,
                                   msg="the return term is not the connector the route builds")

    def test_a_plan_with_no_anchors_simply_drops_the_boundary_terms(self):
        plan = _plan_of(_gen(_inputs([CENTRE_ZONE])))
        self.assertEqual(plan["entry_boundary_m"], 0.0)
        self.assertEqual(plan["return_boundary_m"], 0.0)

    def test_the_approach_and_return_geometry_are_untouched(self):
        """Boundary conditions are READ. No approach or return waypoint may be moved by them."""
        approach = [_m2ll(-40, -20), _m2ll(-20, -10)]
        returns = [_m2ll(220, 130), _m2ll(240, 150)]
        inp = _inputs([CENTRE_ZONE], home=SW_ANCHOR, approach=approach, returns=returns)
        pkg = _gen(inp)
        echoed = pkg["planning_inputs"]
        self.assertEqual([[round(c, 7) for c in p] for p in echoed["approach_waypoints"]],
                         [[round(c, 7) for c in p] for p in approach])
        self.assertEqual([[round(c, 7) for c in p] for p in echoed["return_waypoints"]],
                         [[round(c, 7) for c in p] for p in returns])


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestCoverageIsAnInputNotAnOutput(unittest.TestCase):
    """The invariant everything else is subordinate to: ordering may not move the survey."""

    def test_every_cell_is_visited_exactly_once(self):
        for name in MATRIX:
            with self.subTest(name):
                pkg = _gen(MATRIX[name])
                plan = _plan_of(pkg)
                self.assertEqual(sorted(plan["cell_order"]), list(range(plan["cell_count"])))
                # …and completely, before the route moves on: each cell's fragments are one
                # unbroken run of the execution order.
                runs = []
                for f in pkg["route_quality"]["coverage_fragments"]:
                    if not runs or runs[-1] != f["cell_index"]:
                        runs.append(f["cell_index"])
                self.assertEqual(runs, plan["cell_order"])

    def test_every_coverage_fragment_is_executed_exactly_once(self):
        for name in MATRIX:
            with self.subTest(name):
                _n, _grid, frame, fragments, cells = _geometry_of(MATRIX[name])
                emitted = _emitted_fragment_keys(_gen(MATRIX[name]))
                self.assertEqual(len(emitted), len(fragments))
                self.assertEqual(len(set(emitted)), len(emitted), "a fragment was flown twice")

    def test_the_emitted_fragment_coordinates_are_the_clipped_lane_fragments(self):
        """15: byte-identical, whatever order they end up being flown in."""
        for name in MATRIX:
            with self.subTest(name):
                _n, _grid, frame, _frags, cells = _geometry_of(MATRIX[name])
                self.assertEqual(sorted(_emitted_fragment_keys(_gen(MATRIX[name])), key=repr),
                                 sorted(_clipped_fragment_keys(frame, cells), key=repr))

    def test_cell_membership_is_exactly_the_decomposition_s(self):
        """16: the cell id on every emitted fragment is the cell `_bcd_cells` put it in."""
        for name in MATRIX:
            with self.subTest(name):
                _n, _grid, frame, _frags, cells = _geometry_of(MATRIX[name])
                expected = {}
                for cell_id, cell in enumerate(cells):
                    for frag in cell:
                        a = tuple(round(c, 7) for c in frame.rot_to_deg(frag["rot"][0]))
                        b = tuple(round(c, 7) for c in frame.rot_to_deg(frag["rot"][1]))
                        expected[tuple(sorted([a, b]))] = cell_id
                for cell_id, key in _emitted_fragment_keys(_gen(MATRIX[name])):
                    self.assertEqual(cell_id, expected[key],
                                     "a fragment was reported in a different cell than the "
                                     "decomposition put it in")

    def test_moving_the_entry_anchor_changes_the_order_and_no_byte_of_coverage(self):
        a = _gen(_inputs([CENTRE_ZONE], home=SW_ANCHOR))
        b = _gen(_inputs([CENTRE_ZONE], home=NE_ANCHOR))
        self.assertNotEqual(_plan_of(a)["cell_order"], _plan_of(b)["cell_order"],
                            "the fixture must actually produce two different sequences")
        self.assertEqual(sorted(_emitted_fragment_keys(a), key=repr),
                         sorted(_emitted_fragment_keys(b), key=repr))
        self.assertAlmostEqual(a["route_quality"]["coverage_fragment_length_m"],
                               b["route_quality"]["coverage_fragment_length_m"], delta=0.01)
        for key in ("coverage_fragment_count", "coverage_cell_count",
                    "skipped_short_fragment_count", "non_survey_aligned_coverage_segment_count"):
            self.assertEqual(a["route_quality"][key], b["route_quality"][key], key)

    def test_the_survey_stays_a_lawnmower(self):
        """Coverage must remain straight parallel passes at the survey angle. The ordering may
        take the cells in any order it likes; it may not turn the SURVEY into free-form motion."""
        for name in MATRIX:
            with self.subTest(name):
                inp = MATRIX[name]
                pkg = _gen(inp)
                rq = pkg["route_quality"]
                self.assertEqual(rq["non_survey_aligned_coverage_segment_count"], 0)
                self.assertEqual(rq["same_lane_obstacle_bridge_count"], 0)
                self.assertEqual(rq["fragment_reorders"], 0)
                for f in rq["coverage_fragments"]:
                    self.assertEqual(f["point_count"], 2, "a coverage fragment is not straight")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheContractsUnderneathAreUntouched(unittest.TestCase):
    """17 + 18 + 19 + 20."""

    def test_geometry_validation_stays_green(self):
        for name in MATRIX:
            with self.subTest(name):
                pkg = _gen(MATRIX[name])
                self.assertTrue(pkg["geometry_check"]["ok"],
                                f"[{name}] {pkg['geometry_check']['failures']}")
                # The independent pre-upload validation, run over the package generation just
                # produced — the same route, checked again from the outside.
                verdict = planning.validate_plan(
                    {**copy.deepcopy(MATRIX[name]), "segments": pkg["segments"],
                     "route_waypoints": pkg["route_waypoints"]},
                    max_route_waypoints=5000)
                self.assertTrue(verdict["ok"], f"[{name}] {verdict['errors']}")

    def test_the_sequence_and_the_route_hash_are_deterministic(self):
        for name in MATRIX:
            with self.subTest(name):
                runs = [_gen(MATRIX[name]) for _ in range(3)]
                for pkg in runs[1:]:
                    self.assertEqual(pkg["route_hash"], runs[0]["route_hash"])
                    self.assertEqual(_plan_of(pkg)["cell_order"],
                                     _plan_of(runs[0])["cell_order"])
                    self.assertEqual(_plan_of(pkg)["cell_orientations"],
                                     _plan_of(runs[0])["cell_orientations"])
                    self.assertEqual(pkg["route_quality"], runs[0]["route_quality"])

    def test_the_transition_hierarchy_still_answers_f2_then_f4(self):
        for name in MATRIX:
            with self.subTest(name):
                rq = _gen(MATRIX[name])["route_quality"]
                built = (rq["direct_transit_transition_count"]
                         + rq["shortest_safe_transition_count"]
                         + rq["aligned_direct_transition_count"]
                         + rq["orthogonal_transition_count"])
                # Every consecutive pair of fragments is joined by exactly one transition, and
                # every one of them came out of the hierarchy — nothing is unaccounted for.
                self.assertEqual(built + rq["fallback_connector_count"],
                                 rq["coverage_fragment_count"] - 1,
                                 f"[{name}] the transition tally does not account for the route")
                self.assertGreater(built, 0, "no transition was built at all")
                self.assertGreater(rq["direct_transit_transition_count"], 0,
                                   "the direct-safe tier stopped answering")

    def test_no_hand_over_falls_through_to_the_generic_a_star(self):
        """20: the fallback penalty is a contract term, and this is the contract."""
        for name in MATRIX:
            with self.subTest(name):
                self.assertEqual(_gen(MATRIX[name])["route_quality"]["fallback_connector_count"],
                                 0, f"[{name}] a transition dropped to the generic A* connector")

    def test_the_ordering_beats_the_plain_geometric_order_it_replaced(self):
        """The headline: measured against the stable geometric cell order, on the same geometry,
        with the same cost model. Never worse anywhere, and better where it matters."""
        improved = 0
        for name, inp in MATRIX.items():
            frame, cells, model = _cost_model_of(inp)
            n = len(cells)
            if n < 2:
                continue
            with self.subTest(name):
                order, combo, _diag = _chosen_sequence(frame, cells, model, inp)
                chosen = _sequence_cost(model, n, order, combo)
                # `cells` is already in the geometric key order, entered ascending/forward —
                # which is what the sequence used to be.
                geometric = _sequence_cost(model, n, list(range(n)), [0] * n)
                self.assertLessEqual(chosen, geometric + 1e-6,
                                     f"[{name}] the optimiser is worse than the order it "
                                     f"replaced")
                if chosen < geometric - 1e-6:
                    improved += 1
        self.assertGreater(improved, 0)


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestBoundedModes(unittest.TestCase):
    """Exact below the threshold, deterministic and bounded above it."""

    def test_every_fixture_lands_in_the_exact_mode(self):
        for name in MATRIX:
            with self.subTest(name):
                plan = _plan_of(_gen(MATRIX[name]))
                self.assertLessEqual(plan["cell_count"], planning.BCD_EXACT_MAX_CELLS)
                self.assertEqual(plan["mode"], "exact-held-karp")

    def test_the_heuristic_mode_still_produces_a_legal_plan(self):
        """Forced on, so the path that only runs on an unusually fragmented survey is covered."""
        was = planning.BCD_EXACT_MAX_CELLS
        planning.BCD_EXACT_MAX_CELLS = 1
        try:
            for name in ("central no-go", "two no-go zones", "irregular polygon, two zones"):
                with self.subTest(name):
                    pkg = _gen(MATRIX[name])
                    plan = _plan_of(pkg)
                    self.assertEqual(plan["mode"], "topology-aware-heuristic")
                    self.assertEqual(sorted(plan["cell_order"]),
                                     list(range(plan["cell_count"])))
                    self.assertEqual(pkg["route_quality"]["fallback_connector_count"], 0)
                    self.assertTrue(pkg["geometry_check"]["ok"])
                    # Same coverage, whichever search chose the order.
                    _n, _grid, frame, _frags, cells = _geometry_of(MATRIX[name])
                    self.assertEqual(sorted(_emitted_fragment_keys(pkg), key=repr),
                                     sorted(_clipped_fragment_keys(frame, cells), key=repr))
        finally:
            planning.BCD_EXACT_MAX_CELLS = was

    def test_the_heuristic_mode_is_deterministic(self):
        was = planning.BCD_EXACT_MAX_CELLS
        planning.BCD_EXACT_MAX_CELLS = 1
        try:
            runs = [_gen(MATRIX["two no-go zones"]) for _ in range(2)]
            self.assertEqual(runs[0]["route_hash"], runs[1]["route_hash"])
        finally:
            planning.BCD_EXACT_MAX_CELLS = was


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestPerformance(unittest.TestCase):
    """Operator planning is a click, not a batch job."""

    def test_generation_stays_interactive_on_every_geometry(self):
        for name in MATRIX:
            with self.subTest(name):
                _gen(MATRIX[name])                     # warm the lazy geometry
                t0 = time.perf_counter()
                pkg = _gen(MATRIX[name])
                elapsed = time.perf_counter() - t0
                self.assertLess(elapsed, 2.0,
                                f"[{name}] generation took {elapsed:.2f} s")
                # The measurements are bounded too: the search must never approach the dense
                # n·(n-1)·16 matrix a plain Held-Karp would have needed.
                plan = _plan_of(pkg)
                n = plan["cell_count"]
                self.assertLessEqual(plan["transition_evaluations"], max(1, n * (n - 1) * 16))


if __name__ == "__main__":
    unittest.main()
