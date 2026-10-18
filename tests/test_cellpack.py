import contextlib
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cellpack  # noqa: E402
from cellpack import (  # noqa: E402
    Cell,
    Filters,
    InputError,
    apply_filters,
    arrange,
    best_window,
    build_pack,
    parse_inventory,
    solve,
)

EXAMPLE = ROOT / "examples" / "cells.txt"


def make_cell(cid, cap=3000.0, ir=20.0, v=3.6, imax=5.0, w=0.0, cost="0"):
    return Cell(id=cid, line=1, cap=cap, ir=ir, v=v, w=w, cost=Decimal(cost), imax=imax)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cellpack.main(argv)
    return code, out.getvalue(), err.getvalue()


class ParseTests(unittest.TestCase):
    def test_parses_fields_and_optionals(self):
        cells, errors = parse_inventory("A1 cap=3000 ir=18.5 v=3.62 w=46 cost=1.80 imax=5\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(cells), 1)
        cell = cells[0]
        self.assertEqual((cell.id, cell.line, cell.cap, cell.ir, cell.v), ("A1", 1, 3000.0, 18.5, 3.62))
        self.assertEqual(cell.w, 46.0)
        self.assertEqual(cell.cost, Decimal("1.80"))
        self.assertEqual(cell.imax, 5.0)

    def test_comments_and_blank_lines_keep_line_numbers(self):
        text = "# header\n\nB2 cap=2900 ir=20 v=3.6  # trailing note\n"
        cells, errors = parse_inventory(text)
        self.assertEqual(errors, [])
        self.assertEqual(cells[0].line, 3)

    def test_missing_required_key_is_reported_with_line(self):
        cells, errors = parse_inventory("ok cap=3000 ir=20 v=3.6\nbad cap=3000 v=3.6\n")
        self.assertEqual(len(cells), 1)
        self.assertEqual(errors, [(2, "missing ir")])

    def test_unknown_repeated_and_malformed_tokens(self):
        _, errors = parse_inventory("X1 cap=3000 ir=20 v=3.6 colour=red\n"
                                    "X2 cap=3000 cap=3100 ir=20 v=3.6\n"
                                    "X3 cap3000 ir=20 v=3.6\n")
        messages = [message for _, message in errors]
        self.assertIn("unknown key 'colour'", messages)
        self.assertIn("repeated key 'cap'", messages)
        self.assertIn("expected key=value, got 'cap3000'", messages)

    def test_out_of_range_and_non_numeric_values(self):
        _, errors = parse_inventory("N1 cap=0 ir=20 v=3.6\n"
                                    "N2 cap=3000 ir=abc v=3.6\n"
                                    "N3 cap=3000 ir=nan v=3.6\n"
                                    "N4 cap=3000 ir=20 v=9\n")
        lines = {line for line, _ in errors}
        self.assertEqual(lines, {1, 2, 3, 4})
        messages = " ".join(message for _, message in errors)
        self.assertIn("cap=0 is outside the allowed range", messages)
        self.assertIn("ir must be a number", messages)
        self.assertIn("ir must be finite", messages)
        self.assertIn("v=9 is outside the allowed range", messages)

    def test_duplicate_and_invalid_ids(self):
        cells, errors = parse_inventory("A1 cap=3000 ir=20 v=3.6\n"
                                        "A1 cap=3000 ir=20 v=3.6\n"
                                        "-bad cap=3000 ir=20 v=3.6\n")
        self.assertEqual(len(cells), 1)
        self.assertEqual(errors[0][0], 2)
        self.assertIn("first on line 1", errors[0][1])
        self.assertEqual(errors[1][0], 3)


class FilterTests(unittest.TestCase):
    def test_reasons_are_recorded_and_eligible_cells_kept(self):
        cells = [make_cell("ok"), make_cell("lowcap", cap=2400), make_cell("hiir", ir=40)]
        eligible, rejected = apply_filters(cells, Filters(min_cap=2800, max_ir=25))
        self.assertEqual([c.id for c in eligible], ["ok"])
        self.assertEqual([c.id for c, _ in rejected], ["lowcap", "hiir"])
        self.assertIn("below 2800", rejected[0][1])
        self.assertIn("above 25", rejected[1][1])


class CoreMathTests(unittest.TestCase):
    def test_best_window_picks_tightest_capacity_cluster(self):
        caps = [1000, 1010, 1020, 2000, 2005, 2010, 3000]
        cells = [make_cell(f"c{i}", cap=c) for i, c in enumerate(caps)]
        window = best_window(cells, 3)
        self.assertEqual(sorted(c.cap for c in window), [2000, 2005, 2010])

    def test_arrange_snakes_to_equal_position_sums(self):
        cells = [make_cell(f"c{i}", cap=i) for i in range(1, 9)]
        groups = arrange(cells, series=2, parallel=4)
        self.assertEqual(len(groups), 2)
        self.assertEqual([len(g.cells) for g in groups], [4, 4])
        self.assertEqual(groups[0].cap, groups[1].cap)
        used = sorted(c.id for g in groups for c in g.cells)
        self.assertEqual(used, sorted(c.id for c in cells))

    def test_build_pack_voltage_capacity_and_energy(self):
        cells = [make_cell(f"c{i}", cap=3000, v=3.6) for i in range(6)]
        pack = build_pack(cells, series=3, parallel=2, charge_cell_v=4.2, cutoff_cell_v=2.5)
        self.assertAlmostEqual(pack.nominal_v, 10.8)
        self.assertAlmostEqual(pack.charge_v, 12.6)
        self.assertAlmostEqual(pack.cutoff_v, 7.5)
        self.assertAlmostEqual(pack.ah, 6.0)
        self.assertAlmostEqual(pack.wh, 64.8)
        self.assertEqual(pack.spread_pct, 0)

    def test_parallel_ir_and_pack_ir(self):
        cells = [make_cell("a", ir=20), make_cell("b", ir=20), make_cell("c", ir=30), make_cell("d", ir=30)]
        pack = build_pack(cells, series=2, parallel=2)
        # Each position pairs one 20 and one 30 mOhm cell: 1 / (1/20 + 1/30) = 12 mOhm.
        for group in pack.groups:
            self.assertAlmostEqual(group.ir, 12.0)
        self.assertAlmostEqual(pack.pack_ir, 24.0)

    def test_max_current_uses_weakest_group_and_none_when_unknown(self):
        cells = [make_cell(f"c{i}", imax=5) for i in range(4)]
        self.assertAlmostEqual(build_pack(cells, 2, 2).max_current, 10.0)
        cells[0] = make_cell("c0", imax=None)
        self.assertIsNone(build_pack(cells, 2, 2).max_current)

    def test_load_report_current_and_runtime(self):
        cells = [make_cell(f"c{i}", cap=3000, v=3.6) for i in range(2)]
        pack = build_pack(cells, series=2, parallel=1)
        current, runtime = pack.load_report(72.0)
        self.assertAlmostEqual(current, 10.0)
        self.assertAlmostEqual(runtime, 0.3)

    def test_cost_and_weight_totals_are_exact(self):
        cells = [make_cell("a", cost="0.10", w=10), make_cell("b", cost="0.20", w=15)]
        pack = build_pack(cells, series=2, parallel=1)
        self.assertEqual(pack.cost, Decimal("0.30"))
        self.assertAlmostEqual(pack.weight_kg, 0.025)

    def test_build_pack_rejects_impossible_requests(self):
        cells = [make_cell(f"c{i}") for i in range(3)]
        with self.assertRaises(InputError):
            build_pack(cells, series=2, parallel=2)
        with self.assertRaises(InputError):
            build_pack(cells, series=0, parallel=1)

    def test_solve_respects_voltage_window_spread_and_energy_order(self):
        cells = [make_cell(f"c{i}", cap=3000 + (i % 3) * 10, v=3.6) for i in range(24)]
        options = solve(cells, target_v=10.8, tolerance_pct=5, max_parallel=4, max_spread=5)
        self.assertTrue(options)
        for pack in options:
            self.assertLessEqual(abs(pack.nominal_v - 10.8) / 10.8 * 100, 5 + 1e-9)
            self.assertLessEqual(pack.spread_pct, 5)
        wh = [pack.wh for pack in options]
        self.assertEqual(wh, sorted(wh, reverse=True))


class CliTests(unittest.TestCase):
    def setUp(self):
        # Keep every file the tests create inside a temporary folder in the project.
        scratch = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)

    def write_inventory(self, text):
        path = self.scratch / "inventory.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_check_passes_example_file(self):
        code, out, _ = run_cli(["check", str(EXAMPLE)])
        self.assertEqual(code, 0)
        self.assertIn("OK: 56 cell(s)", out)

    def test_check_reports_file_line_errors_with_exit_2(self):
        path = self.write_inventory("A1 cap=3000 ir=20\n")
        code, _, err = run_cli(["check", path])
        self.assertEqual(code, 2)
        self.assertIn(f"{path}:1: missing v", err)

    def test_build_example_writes_csv_and_json(self):
        csv_path = str(self.scratch / "sheet.csv")
        code, out, _ = run_cli(["build", str(EXAMPLE), "-s", "13", "-p", "4",
                                "--min-mah", "2800", "--max-ir", "25", "--csv", csv_path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["series"], payload["parallel"]), (13, 4))
        self.assertEqual(len(payload["rejected"]), 4)
        self.assertEqual(sum(len(g["cells"]) for g in payload["groups"]), 52)
        with open(csv_path, encoding="utf-8") as handle:
            self.assertEqual(handle.readline().strip(), "position,cells,capacity_mah,ir_mohm,volts")

    def test_build_gate_fails_when_load_exceeds_continuous_rating(self):
        path = self.write_inventory("a cap=3000 ir=20 v=3.6 imax=5\nb cap=3000 ir=20 v=3.6 imax=5\n")
        code, out, _ = run_cli(["build", path, "-s", "2", "-p", "1", "--load", "100"])
        self.assertEqual(code, 1)
        self.assertIn("GATE FAILED", out)

    def test_build_gate_passes_for_light_load(self):
        path = self.write_inventory("a cap=3000 ir=20 v=3.6 imax=5\nb cap=3000 ir=20 v=3.6 imax=5\n")
        # 10 W at 7.2 V is about 1.39 A, so a 3 Ah pack lasts roughly 2.2 h: below a 3 h minimum.
        code, out, _ = run_cli(["build", path, "-s", "2", "-p", "1", "--load", "10", "--min-runtime", "3"])
        self.assertEqual(code, 1)
        self.assertIn("below minimum", out)
        code, out, _ = run_cli(["build", path, "-s", "2", "-p", "1", "--load", "5"])
        self.assertEqual(code, 0)
        self.assertNotIn("GATE FAILED", out)

    def test_solve_finds_builds_for_target_voltage(self):
        code, out, _ = run_cli(["solve", str(EXAMPLE), "--target-v", "48", "--min-mah", "2800",
                                "--max-ir", "25", "--top", "3"])
        self.assertEqual(code, 0)
        self.assertIn("Top", out)
        self.assertIn("S", out)

    def test_bad_build_request_exits_2(self):
        code, _, err = run_cli(["build", str(EXAMPLE), "-s", "40", "-p", "4"])
        self.assertEqual(code, 2)
        self.assertIn("needs 160 cells", err)


if __name__ == "__main__":
    unittest.main()
