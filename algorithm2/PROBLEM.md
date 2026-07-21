# Problem specification — post-combustion CO2 membrane capture plant design

You are designing a membrane-based CO2 capture plant. This document is the complete
problem statement: the feed, the targets, the equipment you may use, and how a design
is written down and evaluated. Everything else you need must come from your own
chemical-engineering knowledge and from the measured results the evaluator returns.

## Feed and targets

- Feed: post-combustion flue gas, **13 mol% CO2** (rest N2), 80,307 kmol/h, 1.0 bar, continuous.
- Hard constraints: product **CO2 purity >= 0.95** and **CO2 recovery >= 0.90** (mole basis).
- Objective: among designs meeting both constraints, **minimize the capture cost [$/tCO2]**.

## Plant rules (fixed by the campaign — not yours to change)

- The whole flue gas is NOT compressed. Every membrane feed is boosted by a **blower to
  1.1 bar** (fixed; inserted automatically at every membrane inlet).
- Driving force comes from **vacuum on the permeate side**: you choose each membrane's
  permeate pressure within [0.1, 0.99] bar. A vacuum pump restoring the permeate to
  1.0 bar is inserted automatically, as are 35 °C coolers after rotating machines.
- Recycled streams (sent back to a membrane) join that membrane's feed mixer at ~1 bar;
  no re-compression is needed.

## Membranes

Each membrane you place has three free parameters:

| parameter | range | note |
|---|---|---|
| `area_m2` | 1.0e5 – 1.5e6 | per membrane unit |
| `p_permeate_bar` | 0.1 – 0.99 | permeate-side vacuum level |
| `permeance_gpu` | 500 – 6000 | CO2 permeance |

The CO2/N2 selectivity is **not** a free parameter: every membrane lies on the 2008
Robeson upper bound, so selectivity is set automatically from the permeance
(monotonically decreasing: ~500 GPU -> alpha ~ 99, ~6000 GPU -> alpha ~ 43).
High-flux membranes are less selective, and vice versa.

## Economics (used by the evaluator; annualized)

- Membrane: 50 $/m2. Blower: 670 $/kW. Vacuum pump: 1341 $/kW. Cooler: 300 $/m2.
- Installation factor 1.6, capital charge rate 0.2 /yr, electricity 0.04 $/kWh,
  7,446 operating h/yr.
- cost [$/tCO2] = annualized capital / captured CO2 + specific energy x electricity price.

## How to write a design (proposal JSON)

```json
{
  "membranes": {
    "M1": {"area_m2": 700000, "p_permeate_bar": 0.1, "permeance_gpu": 5000},
    "M2": {"area_m2": 550000, "p_permeate_bar": 0.3, "permeance_gpu": 900}
  },
  "streams": {
    "feed": "M1",
    "M1.permeate": "M2",
    "M1.retentate": "residue",
    "M2.permeate": "product",
    "M2.retentate": "M1"
  }
}
```

- `membranes`: 1–8 membranes, alphanumeric names, three parameters each.
- `streams`: where every flow goes. Required keys: `feed`, and `<name>.permeate` /
  `<name>.retentate` for every membrane. Each destination is another membrane's name
  (the stream joins its feed mixer), `"product"`, or `"residue"`.
- Every stream has exactly one destination (no splitting). At least one stream must
  reach `product` and one must reach `residue`, and every membrane must receive flow.

## How to evaluate

```
uv run python evaluate.py --dry-validate proposal.json          # free wiring/schema check
uv run python evaluate.py --run-dir runs/direct01 proposal.json # real simulation (~10-60 s)
```

A successful evaluation returns purity, recovery, feasibility, cost, specific energy,
the per-unit energy breakdown, and the composition/flow of every internal stream.
A design the simulator cannot converge is reported as a failed (bad) point — treat it
as information, not as a crash.

## Budget

You have a **6.5-hour wall-clock budget per run**, starting at your first real
evaluation. The evaluator refuses further evaluations once it is spent. Your thinking
time counts: the clock does not pause between evaluations. `--dry-validate` is free.
