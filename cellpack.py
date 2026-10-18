#!/usr/bin/env python3
"""cellpack - plan balanced battery packs from a plain-text cell inventory.

Each non-blank line of the inventory is a cell id followed by key=value pairs:

    A1 cap=3000 ir=18 v=3.62 w=46 cost=2.10 imax=5

Required keys: cap (mAh), ir (milliohms), v (nominal volts).
Optional keys: w (grams), cost (currency, exact), imax (continuous amps).
Text after "#" is a comment. Exit codes: 0 ok, 1 gate failed, 2 bad input or usage.
"""
import argparse
import csv
import io
import json
import math
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

REQUIRED = ("cap", "ir", "v")
RANGES = {
    "cap": (1, 100_000),
    "ir": (0.1, 1_000),
    "v": (0.5, 5.0),
    "w": (0, 10_000),
    "cost": (0, 100_000),
    "imax": (0.01, 1_000),
}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class InputError(Exception):
    """Raised for inventory or request problems that cannot be planned."""


@dataclass(frozen=True)
class Cell:
    id: str
    line: int
    cap: float
    ir: float
    v: float
    w: float = 0.0
    cost: Decimal = Decimal("0")
    imax: float | None = None


@dataclass(frozen=True)
class Filters:
    min_cap: float | None = None
    max_ir: float | None = None
    v_min: float | None = None
    v_max: float | None = None


@dataclass(frozen=True)
class Group:
    """One series position: the cells wired in parallel there."""

    position: int
    cells: tuple

    @property
    def cap(self):
        return sum(c.cap for c in self.cells)

    @property
    def ir(self):
        return 1 / sum(1 / c.ir for c in self.cells)

    @property
    def v(self):
        return sum(c.v for c in self.cells) / len(self.cells)

    @property
    def imax(self):
        if any(c.imax is None for c in self.cells):
            return None
        return sum(c.imax for c in self.cells)

    @property
    def ids(self):
        return [c.id for c in self.cells]


@dataclass(frozen=True)
class Pack:
    series: int
    parallel: int
    groups: tuple
    charge_cell_v: float
    cutoff_cell_v: float

    @property
    def nominal_v(self):
        return sum(g.v for g in self.groups)

    @property
    def charge_v(self):
        return self.series * self.charge_cell_v

    @property
    def cutoff_v(self):
        return self.series * self.cutoff_cell_v

    @property
    def ah(self):
        """Series strings are limited by their weakest parallel group."""
        return min(g.cap for g in self.groups) / 1000

    @property
    def wh(self):
        return self.ah * self.nominal_v

    @property
    def pack_ir(self):
        return sum(g.ir for g in self.groups)

    @property
    def spread_pct(self):
        caps = [g.cap for g in self.groups]
        mean = sum(caps) / len(caps)
        return (max(caps) - min(caps)) / mean * 100

    @property
    def max_current(self):
        limits = [g.imax for g in self.groups]
        if any(limit is None for limit in limits):
            return None
        return min(limits)

    @property
    def cells(self):
        return [c for g in self.groups for c in g.cells]

    @property
    def weight_kg(self):
        return sum(c.w for c in self.cells) / 1000

    @property
    def cost(self):
        return sum((c.cost for c in self.cells), Decimal("0"))

    def load_report(self, load_w):
        """Return (current_a, runtime_h) for a constant-power load."""
        current = load_w / self.nominal_v
        return current, self.ah / current


def _number(key, text):
    try:
        value = Decimal(text) if key == "cost" else float(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{key} must be a number, got {text!r}")
    finite = value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)
    if not finite:
        raise ValueError(f"{key} must be finite, got {text!r}")
    low, high = RANGES[key]
    if not low <= value <= high:
        raise ValueError(f"{key}={text} is outside the allowed range {low}..{high}")
    return value


def parse_inventory(text):
    """Return (cells, errors). errors is a list of (line number, message)."""
    cells, errors, first_seen = [], [], {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        tokens = raw.split("#", 1)[0].split()
        if not tokens:
            continue
        cid, fields = tokens[0], tokens[1:]
        if not ID_RE.fullmatch(cid):
            errors.append((lineno, f"invalid cell id {cid!r}"))
            continue
        if cid in first_seen:
            errors.append((lineno, f"duplicate cell id {cid!r} (first on line {first_seen[cid]})"))
            continue
        first_seen[cid] = lineno
        values, problems = {}, []
        for token in fields:
            key, sep, value_text = token.partition("=")
            if not sep or not value_text:
                problems.append(f"expected key=value, got {token!r}")
            elif key not in RANGES:
                problems.append(f"unknown key {key!r}")
            elif key in values:
                problems.append(f"repeated key {key!r}")
            else:
                try:
                    values[key] = _number(key, value_text)
                except ValueError as exc:
                    problems.append(str(exc))
        missing = [key for key in REQUIRED if key not in values]
        if missing:
            problems.append("missing " + ", ".join(missing))
        if problems:
            errors.extend((lineno, message) for message in problems)
            continue
        cells.append(Cell(
            id=cid,
            line=lineno,
            cap=float(values["cap"]),
            ir=float(values["ir"]),
            v=float(values["v"]),
            w=float(values.get("w", 0.0)),
            cost=values.get("cost", Decimal("0")),
            imax=float(values["imax"]) if "imax" in values else None,
        ))
    return cells, errors


def apply_filters(cells, filters):
    """Split cells into (eligible, rejected) where rejected holds (cell, reason)."""
    eligible, rejected = [], []
    for cell in cells:
        reasons = []
        if filters.min_cap is not None and cell.cap < filters.min_cap:
            reasons.append(f"cap {cell.cap:g} below {filters.min_cap:g}")
        if filters.max_ir is not None and cell.ir > filters.max_ir:
            reasons.append(f"ir {cell.ir:g} above {filters.max_ir:g}")
        if filters.v_min is not None and cell.v < filters.v_min:
            reasons.append(f"v {cell.v:g} below {filters.v_min:g}")
        if filters.v_max is not None and cell.v > filters.v_max:
            reasons.append(f"v {cell.v:g} above {filters.v_max:g}")
        if reasons:
            rejected.append((cell, "; ".join(reasons)))
        else:
            eligible.append(cell)
    return eligible, rejected


def best_window(cells, count):
    """Pick the `count` cells with the tightest capacity, then IR, spread."""
    ordered = sorted(cells, key=lambda c: (c.cap, c.id))
    best = None
    for start in range(len(ordered) - count + 1):
        window = ordered[start:start + count]
        caps = [c.cap for c in window]
        irs = [c.ir for c in window]
        key = (max(caps) - min(caps), max(irs) - min(irs), -sum(caps), start)
        if best is None or key < best[0]:
            best = (key, window)
    return best[1]


def arrange(window, series, parallel):
    """Snake the sorted cells so every series position gets a balanced mix."""
    ordered = sorted(window, key=lambda c: (-c.cap, c.id))
    slots = [[] for _ in range(series)]
    for row in range(parallel):
        chunk = ordered[row * series:(row + 1) * series]
        order = range(series) if row % 2 == 0 else range(series - 1, -1, -1)
        for slot, cell in zip(order, chunk):
            slots[slot].append(cell)
    return tuple(Group(i + 1, tuple(cells)) for i, cells in enumerate(slots))


def build_pack(cells, series, parallel, charge_cell_v=4.2, cutoff_cell_v=2.5):
    if series < 1 or parallel < 1:
        raise InputError("series and parallel counts must be at least 1")
    needed = series * parallel
    if needed > len(cells):
        raise InputError(f"{series}S{parallel}P needs {needed} cells but only {len(cells)} are eligible")
    window = best_window(cells, needed)
    return Pack(series, parallel, arrange(window, series, parallel), charge_cell_v, cutoff_cell_v)


def solve(cells, target_v, tolerance_pct, max_parallel, max_spread,
          charge_cell_v=4.2, cutoff_cell_v=2.5):
    """Return every SxP pack near target_v and within max_spread, best energy first."""
    mean_v = sum(c.v for c in cells) / len(cells)
    lower = target_v * (1 - tolerance_pct / 100)
    upper = target_v * (1 + tolerance_pct / 100)
    results = []
    for series in range(1, len(cells) + 1):
        nominal = series * mean_v
        if nominal > upper:
            break
        if nominal < lower:
            continue
        for parallel in range(1, max_parallel + 1):
            if series * parallel > len(cells):
                break
            pack = build_pack(cells, series, parallel, charge_cell_v, cutoff_cell_v)
            if pack.spread_pct <= max_spread:
                results.append(pack)
    results.sort(key=lambda p: (-p.wh, p.spread_pct, p.series, p.parallel))
    return results


def format_pack(pack, load_w=None, min_runtime_h=None):
    """Return (text, gate_failures) for a pack report."""
    lines = [
        f"Pack {pack.series}S{pack.parallel}P ({len(pack.cells)} cells)",
        f"  Nominal {pack.nominal_v:.2f} V   charge {pack.charge_v:.1f} V   cutoff {pack.cutoff_v:.1f} V",
        f"  Capacity {pack.ah:.2f} Ah   energy {pack.wh:.1f} Wh   pack IR {pack.pack_ir:.1f} mOhm",
        f"  Balance: group capacity spread {pack.spread_pct:.2f}%",
    ]
    if pack.max_current is None:
        lines.append("  Max continuous current: unknown (some cells have no imax=)")
    else:
        lines.append(f"  Max continuous current {pack.max_current:.1f} A")
    lines.append(f"  Weight {pack.weight_kg:.2f} kg   cost {pack.cost:.2f}")
    failures = []
    if load_w is not None:
        current, runtime = pack.load_report(load_w)
        lines.append(f"  Load {load_w:g} W draws {current:.2f} A, runtime {runtime:.2f} h")
        if pack.max_current is not None and current > pack.max_current:
            failures.append(f"load {current:.2f} A exceeds max continuous {pack.max_current:.1f} A")
        if min_runtime_h is not None and runtime < min_runtime_h:
            failures.append(f"runtime {runtime:.2f} h below minimum {min_runtime_h:g} h")
    lines.append("")
    lines.append(f"  {'Pos':>3}  {'Cells':<28} {'Capacity':>10} {'IR mOhm':>8} {'Volts':>7}")
    for group in pack.groups:
        lines.append(
            f"  {group.position:>3}  {' '.join(group.ids):<28} {group.cap:>7.0f} mAh"
            f" {group.ir:>8.2f} {group.v:>7.3f}"
        )
    return "\n".join(lines), failures


def pack_to_dict(pack):
    return {
        "series": pack.series,
        "parallel": pack.parallel,
        "nominal_v": round(pack.nominal_v, 4),
        "charge_v": round(pack.charge_v, 3),
        "cutoff_v": round(pack.cutoff_v, 3),
        "ah": round(pack.ah, 4),
        "wh": round(pack.wh, 3),
        "pack_ir_mohm": round(pack.pack_ir, 3),
        "spread_pct": round(pack.spread_pct, 4),
        "max_current_a": pack.max_current,
        "weight_kg": round(pack.weight_kg, 4),
        "cost": str(pack.cost),
        "groups": [
            {"position": g.position, "cells": g.ids, "cap_mah": g.cap,
             "ir_mohm": round(g.ir, 4), "v": round(g.v, 4)}
            for g in pack.groups
        ],
    }


def build_sheet_csv(pack):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["position", "cells", "capacity_mah", "ir_mohm", "volts"])
    for group in pack.groups:
        writer.writerow([group.position, " ".join(group.ids), f"{group.cap:.0f}",
                         f"{group.ir:.3f}", f"{group.v:.4f}"])
    return buffer.getvalue()


def _load_inventory(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    cells, errors = parse_inventory(text)
    if errors:
        raise InputError("\n".join(f"{path}:{line}: {message}" for line, message in errors))
    if not cells:
        raise InputError(f"{path}: no cells found")
    return cells


def _filters(args):
    return Filters(min_cap=args.min_mah, max_ir=args.max_ir, v_min=args.v_min, v_max=args.v_max)


def _print_rejected(rejected):
    if not rejected:
        return
    print(f"Rejected {len(rejected)} cell(s):")
    for cell, reason in rejected[:10]:
        print(f"  {cell.id} (line {cell.line}): {reason}")
    if len(rejected) > 10:
        print(f"  ... and {len(rejected) - 10} more")


def cmd_check(args):
    cells = _load_inventory(args.inventory)
    print(f"OK: {len(cells)} cell(s) in {args.inventory}")
    return 0


def cmd_build(args):
    cells = _load_inventory(args.inventory)
    eligible, rejected = apply_filters(cells, _filters(args))
    pack = build_pack(eligible, args.series, args.parallel, args.charge_cell_v, args.cutoff_cell_v)
    text, failures = format_pack(pack, args.load, args.min_runtime)
    if args.max_spread is not None and pack.spread_pct > args.max_spread:
        failures.append(f"group spread {pack.spread_pct:.2f}% above {args.max_spread:g}%")
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as handle:
            handle.write(build_sheet_csv(pack))
    if args.json:
        payload = pack_to_dict(pack)
        payload["rejected"] = [{"id": c.id, "line": c.line, "reason": r} for c, r in rejected]
        payload["gate_failures"] = failures
        print(json.dumps(payload, indent=2))
    else:
        _print_rejected(rejected)
        print(text)
        if failures:
            print("\nGATE FAILED: " + "; ".join(failures))
    return 1 if failures else 0


def cmd_solve(args):
    cells = _load_inventory(args.inventory)
    eligible, rejected = apply_filters(cells, _filters(args))
    if not eligible:
        raise InputError("no cells pass the filters")
    options = solve(eligible, args.target_v, args.tolerance, args.max_parallel,
                    args.max_spread, args.charge_cell_v, args.cutoff_cell_v)[:args.top]
    if args.json:
        payload = {
            "target_v": args.target_v,
            "options": [pack_to_dict(p) for p in options],
            "rejected": [{"id": c.id, "line": c.line, "reason": r} for c, r in rejected],
        }
        print(json.dumps(payload, indent=2))
        return 0
    _print_rejected(rejected)
    if not options:
        print(f"No SxP build near {args.target_v:g} V within {args.tolerance:g}% "
              f"and {args.max_spread:g}% spread.")
        return 1
    print(f"Top {len(options)} builds near {args.target_v:g} V, most energy first:")
    for pack in options:
        print(f"  {pack.series}S{pack.parallel}P  {pack.nominal_v:6.2f} V  {pack.ah:6.2f} Ah"
              f"  {pack.wh:7.1f} Wh  spread {pack.spread_pct:5.2f}%  cells {len(pack.cells)}")
    return 0


def _positive_float(text):
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog="cellpack", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("--min-mah", type=_positive_float, help="reject cells below this capacity")
    filters.add_argument("--max-ir", type=_positive_float, help="reject cells above this IR (mOhm)")
    filters.add_argument("--v-min", type=_positive_float, help="reject cells below this voltage")
    filters.add_argument("--v-max", type=_positive_float, help="reject cells above this voltage")
    filters.add_argument("--charge-cell-v", type=_positive_float, default=4.2,
                         help="per-cell charge voltage (default 4.2)")
    filters.add_argument("--cutoff-cell-v", type=_positive_float, default=2.5,
                         help="per-cell discharge cutoff (default 2.5)")
    filters.add_argument("--json", action="store_true", help="print JSON instead of text")

    check = sub.add_parser("check", help="lint an inventory file")
    check.add_argument("inventory")
    check.set_defaults(func=cmd_check)

    build = sub.add_parser("build", parents=[filters], help="build one SxP pack")
    build.add_argument("inventory")
    build.add_argument("-s", "--series", type=int, required=True)
    build.add_argument("-p", "--parallel", type=int, required=True)
    build.add_argument("--load", type=_positive_float, help="constant load in watts to check")
    build.add_argument("--min-runtime", type=_positive_float, help="gate: minimum hours at --load")
    build.add_argument("--max-spread", type=_positive_float, help="gate: max group spread percent")
    build.add_argument("--csv", help="write the build sheet to this CSV path")
    build.set_defaults(func=cmd_build)

    solve_cmd = sub.add_parser("solve", parents=[filters], help="find builds near a target voltage")
    solve_cmd.add_argument("inventory")
    solve_cmd.add_argument("--target-v", type=_positive_float, required=True)
    solve_cmd.add_argument("--tolerance", type=_positive_float, default=5.0,
                           help="allowed voltage error in percent (default 5)")
    solve_cmd.add_argument("--max-parallel", type=int, default=8)
    solve_cmd.add_argument("--max-spread", type=_positive_float, default=5.0,
                           help="drop builds with group spread above this percent (default 5)")
    solve_cmd.add_argument("--top", type=int, default=5)
    solve_cmd.set_defaults(func=cmd_solve)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InputError as exc:
        print(f"cellpack: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
