# cellpack

**Plan balanced battery packs from a plain-text cell inventory, offline.**

cellpack reads a list of salvaged or new cells (capacity, internal resistance, voltage, optional weight, cost and continuous current), filters out the ones you don't want, picks the tightest matched set for an `SxP` build, and reports pack voltage, capacity, energy, balance, current limit, runtime at a load and a build sheet. It is one Python file with the standard library only: no network, no accounts, no API keys.

## Features

- **Plain-text inventory**: one cell per line, `id key=value ...`, with `#` comments.
- **Line-numbered lint**: every bad line is reported as `file:line: message` with exit code 2. Covers missing required keys, unknown or repeated keys, non-numeric or non-finite values, out-of-range values, duplicate ids and invalid ids.
- **Filters with reasons**: reject cells by minimum capacity, maximum IR, and a voltage window. Each rejected cell shows why.
- **Matched selection**: for an `SxP` build it slides over capacity-sorted cells and keeps the window with the smallest capacity spread, then the smallest IR spread.
- **Balanced arrangement**: cells are snake-assigned across the series positions, so each position's parallel group carries a near-equal share of capacity.
- **Balance report**: per-group capacity, parallel IR (`1 / Σ(1/R)`) and mean voltage, plus the capacity spread percentage.
- **Pack electrical summary**: nominal, charge and discharge-cutoff voltage; amp-hours limited by the weakest group; watt-hours; pack IR.
- **Continuous current limit**: derived from each cell's `imax=`, or reported as unknown if any cell lacks it.
- **Runtime at a load**: give a constant load in watts and get the current draw, runtime, and a gate if the draw exceeds the continuous rating.
- **Exact cost and weight**: cost uses `Decimal`, so totals are exact to the cent.
- **Target-voltage solver**: `solve` lists every `SxP` build within a voltage tolerance and spread limit, ranked by energy.
- **CSV build sheet**: one row per series position with the cell ids, capacity, IR and voltage, ready for a bench sheet.
- **JSON output** for both `build` and `solve`, including rejected cells and gate failures.
- **CI-friendly exit codes**: `0` success, `1` a gate failed (spread, load over rating, minimum runtime), `2` bad input or an impossible request.

## Quick start

The repository includes `examples/cells.txt`: 52 usable cells and 4 that the filters below reject.

```bash
python3 cellpack.py check examples/cells.txt

python3 cellpack.py build examples/cells.txt -s 13 -p 4 \
    --min-mah 2800 --max-ir 25 --load 300 --min-runtime 1 \
    --max-spread 2 --csv build-sheet.csv

python3 cellpack.py solve examples/cells.txt --target-v 48 \
    --min-mah 2800 --max-ir 25 --top 3
```

The `build` run prints the rejected cells, the pack summary, and a table with one row per series position. It writes `build-sheet.csv` and exits `0` only if every gate passes.

## Inventory format

```text
# id   cap=mAh  ir=mOhm  v=volts  [w=grams]  [cost=currency]  [imax=amps]
A01    cap=3012 ir=18.2 v=3.62 w=46 cost=1.80 imax=5
```

| Key    | Required | Meaning                                |
|--------|----------|----------------------------------------|
| `cap`  | yes      | rated capacity in mAh (1 to 100000)    |
| `ir`   | yes      | internal resistance in mOhm            |
| `v`    | yes      | nominal voltage (0.5 to 5.0)           |
| `w`    | no       | mass in grams                          |
| `cost` | no       | unit cost, kept exact                  |
| `imax` | no       | continuous discharge current in amps   |

## Commands

| Command | Purpose |
|---------|---------|
| `check FILE` | Lint the inventory and print the cell count. |
| `build FILE -s S -p P` | Plan one `SxP` pack from the eligible cells. |
| `solve FILE --target-v V` | List builds near a target voltage, best energy first. |

Shared options: `--min-mah`, `--max-ir`, `--v-min`, `--v-max`, `--charge-cell-v` (default 4.2), `--cutoff-cell-v` (default 2.5), `--json`.

`build` also takes `--load W`, `--min-runtime H` (checked only with `--load`), `--max-spread PCT`, and `--csv PATH`.

`solve` also takes `--tolerance PCT` (default 5), `--max-parallel N` (default 8), `--max-spread PCT` (default 5) and `--top N` (default 5).

## Requirements

Python 3.10 or newer. No third-party packages.

## Running

Run the script directly from the project folder:

```bash
python3 cellpack.py --help
```

## Testing

The tests use `unittest`, which pytest also runs:

```bash
python3 -m unittest discover -s tests -v
# or
python3 -m pytest tests -q
```

The suite covers parsing and lint errors, filter reasons, window selection, balanced arrangement, pack voltage, capacity and energy, parallel IR, current limits, load runtime, exact cost totals, solver ordering, the CSV and JSON outputs, and the exit codes.

## Notes

cellpack plans from the numbers you give it. It does not check chemistry safety, protection-circuit requirements or regulatory rules. Verify cell ratings and the final pack design against your cell datasheets before building or using a pack.

## Licence

No licence is set in this project.
