"""Pure-function library that extracts signals from results.json.

Aspen-independent. run_iteration.py uses it for display and auto-commit, and the
outer-loop Claude (algorithm/CLAUDE.md = SST agent) reads it as the basis for
structural proposals.

Responsibilities:
    - Detection of continuous variables sitting at their bounds (bounds_hit)
    - Energy breakdown per block (all blocks, share descending + dominant flag)
    - CO2 lost to residue sinks
    - Shortfall against the purity / recovery targets
    - ON/OFF state of candidate arcs
    - extract(), which returns the above as a Signals dataclass, and summarize()
      for display
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from evaluator import BAD_VALUE            # noqa: E402
from topology import continuous_variables  # noqa: E402


# =========================================================
# Module constants (scales for signal extraction, not tuning knobs)
# =========================================================

BOUNDS_HIT_SLACK = 0.05       # counted as bounds-hit within 5% of the lo/hi bound
ENERGY_DOMINANT_SHARE = 0.5   # dominance is defined as share > 0.5


# =========================================================
# Data types
# =========================================================

@dataclass
class BoundsHit:
    name: str          # GA variable name (e.g. "MEMB1_area")
    value: float
    side: str          # "lower" or "upper"
    limit: float       # bound value on the side being hit
    slack_ratio: float  # (v - lo) / span or (hi - v) / span; closer to 0 = tighter hit


@dataclass
class EnergyBlock:
    block: str          # e.g. "VP1"
    kw: float
    share: float        # kw / sum(all kws)
    dominant: bool      # share > ENERGY_DOMINANT_SHARE


@dataclass
class ResidueLoss:
    vid: str            # e.g. "V8"
    description: str    # vertices[vid].label
    co2_moleflow: float
    share_of_feed_co2: float  # residue.moleflow / feed.moleflow (summed ratio taken outside)


@dataclass
class ConstraintViolation:
    purity: float
    recovery: float
    purity_min: float
    recovery_min: float
    purity_short: float    # max(0, purity_min - purity)
    recovery_short: float  # max(0, recovery_min - recovery)

    @property
    def is_violating(self) -> bool:
        return self.purity_short > 0.0 or self.recovery_short > 0.0


@dataclass
class Signals:
    iteration: int
    specific_energy: float
    cost_per_tco2: float | None = None   # $/tCO2 (12.2; Optional since old results lack it)
    bounds_hit: list[BoundsHit] = field(default_factory=list)
    energy_blocks: list[EnergyBlock] = field(default_factory=list)
    residue_losses: list[ResidueLoss] = field(default_factory=list)
    constraint_violation: ConstraintViolation | None = None
    candidate_status: dict[str, int] = field(default_factory=dict)


# =========================================================
# Extraction (individual signals)
# =========================================================

def extract_bounds_hit(
    results: dict, ss: dict, membrane_model: dict | None = None
) -> list[BoundsHit]:
    """Detect continuous variables sitting at their bounds. Threshold: BOUNDS_HIT_SLACK.

    Bounds come from continuous_variables(ss), so bounds_override is applied automatically.
    Passing membrane_model (12.1) also brings permeance variables into scope.
    """
    opt_params: dict[str, Any] = results.get("optimal_params", {}) or {}
    hits: list[BoundsHit] = []
    for cv in continuous_variables(ss, membrane_model):
        name = cv["name"]
        if name not in opt_params:
            continue
        v = opt_params[name]
        if not isinstance(v, (int, float)):
            continue
        lo, hi = cv["bounds"]
        span = hi - lo
        if span <= 0:
            continue
        lower_slack = (v - lo) / span
        upper_slack = (hi - v) / span
        if lower_slack < BOUNDS_HIT_SLACK:
            hits.append(BoundsHit(name=name, value=float(v), side="lower",
                                  limit=float(lo), slack_ratio=float(lower_slack)))
        elif upper_slack < BOUNDS_HIT_SLACK:
            hits.append(BoundsHit(name=name, value=float(v), side="upper",
                                  limit=float(hi), slack_ratio=float(upper_slack)))
    return hits


def extract_energy_blocks(results: dict) -> list[EnergyBlock]:
    """Return the whole energy_breakdown by descending share; dominant flags those above the threshold."""
    breakdown: dict[str, float] = results.get("energy_breakdown", {}) or {}
    total = sum(breakdown.values())
    if total <= 0:
        return []
    blocks = [
        EnergyBlock(
            block=name,
            kw=float(kw),
            share=float(kw) / total,
            dominant=(float(kw) / total) > ENERGY_DOMINANT_SHARE,
        )
        for name, kw in breakdown.items()
    ]
    blocks.sort(key=lambda b: b.share, reverse=True)
    return blocks


def extract_residue_losses(results: dict, ss: dict) -> list[ResidueLoss]:
    """Return the loss share relative to feed CO2 for each role=residue sink vertex.

    The feed is the role=feed vertex (normally a single V0). With multiple feeds the
    CO2 moleflows are summed.
    """
    streams: dict[str, dict] = results.get("stream_results", {}) or {}
    vertices: dict[str, dict] = ss.get("vertices", {}) or {}

    feed_vids = [v for v, d in vertices.items() if d.get("role") == "feed"]
    feed_co2_total = 0.0
    for fv in feed_vids:
        mf = (streams.get(fv) or {}).get("CO2_moleflow")
        if isinstance(mf, (int, float)) and mf > 0:
            feed_co2_total += float(mf)

    losses: list[ResidueLoss] = []
    for vid, vdef in vertices.items():
        if vdef.get("role") != "residue":
            continue
        s = streams.get(vid) or {}
        mf = s.get("CO2_moleflow")
        if not isinstance(mf, (int, float)):
            continue
        share = (float(mf) / feed_co2_total) if feed_co2_total > 0 else 0.0
        losses.append(ResidueLoss(
            vid=vid,
            description=vdef.get("label", vid),
            co2_moleflow=float(mf),
            share_of_feed_co2=float(share),
        ))
    return losses


def extract_constraint_violation(results: dict, case: dict) -> ConstraintViolation:
    perf = results.get("performance", {}) or {}
    targets = case.get("optimization_targets", {}) or {}
    purity   = float(perf.get("CO2_purity", 0.0) or 0.0)
    recovery = float(perf.get("CO2_recovery", 0.0) or 0.0)
    p_min    = float(targets.get("purity_min", 0.0) or 0.0)
    r_min    = float(targets.get("recovery_min", 0.0) or 0.0)
    return ConstraintViolation(
        purity=purity,
        recovery=recovery,
        purity_min=p_min,
        recovery_min=r_min,
        purity_short=max(0.0, p_min - purity),
        recovery_short=max(0.0, r_min - recovery),
    )


# =========================================================
# Aggregation
# =========================================================

def extract(results: dict, ss: dict, case: dict) -> Signals:
    """Assemble Signals from results.json + ss + case."""
    # The cost sentinel (BAD_VALUE) means "could not be measured", not a value, so drop it to None
    _cost = results.get("performance", {}).get("cost_usd_per_tCO2")
    _cost_valid = isinstance(_cost, (int, float)) and _cost < BAD_VALUE
    return Signals(
        iteration=int(results.get("iteration", 0)),
        specific_energy=float(results.get("performance", {}).get("specific_energy_kWh_tCO2", 0.0)),
        cost_per_tco2=float(_cost) if _cost_valid else None,
        bounds_hit=extract_bounds_hit(results, ss, case.get("membrane_model")),
        energy_blocks=extract_energy_blocks(results),
        residue_losses=extract_residue_losses(results, ss),
        constraint_violation=extract_constraint_violation(results, case),
        candidate_status=dict(results.get("active_candidates", {}) or {}),
    )


# =========================================================
# Display
# =========================================================

def summarize(sig: Signals) -> str:
    """Format Signals as text for human readers and the SST agent."""
    lines: list[str] = []
    lines.append(f"========== Signals (iter {sig.iteration}) ==========")

    cv = sig.constraint_violation
    if cv is not None:
        purity_mark   = "OK" if cv.purity_short   == 0.0 else f"SHORT {cv.purity_short*100:.1f}%"
        recovery_mark = "OK" if cv.recovery_short == 0.0 else f"SHORT {cv.recovery_short*100:.1f}%"
        lines.append("Performance:")
        lines.append(f"  CO2 purity:        {cv.purity*100:.1f}%   "
                     f"(target {cv.purity_min*100:.1f}%, {purity_mark})")
        lines.append(f"  CO2 recovery:      {cv.recovery*100:.1f}%   "
                     f"(target {cv.recovery_min*100:.1f}%, {recovery_mark})")
        lines.append(f"  Specific energy:   {sig.specific_energy:.1f} kWh/tCO2")
        if sig.cost_per_tco2 is not None:
            lines.append(f"  Capture cost:      {sig.cost_per_tco2:.2f} $/tCO2")

    lines.append("")
    lines.append("Energy blocks (share desc):")
    if not sig.energy_blocks:
        lines.append("  (no energy data)")
    else:
        for b in sig.energy_blocks:
            marker = " [DOMINANT]" if b.dominant else ""
            lines.append(f"  {b.block:6s}: {b.kw:8.1f} kW ({b.share*100:5.1f}%){marker}")

    lines.append("")
    lines.append("Bounds-hit variables:")
    if not sig.bounds_hit:
        lines.append("  (none)")
    else:
        for h in sig.bounds_hit:
            lines.append(f"  {h.name} = {h.value:.4g}  "
                         f"({h.side} limit {h.limit:.4g}, slack {h.slack_ratio*100:.1f}%)")

    lines.append("")
    lines.append("Residue CO2 loss:")
    if not sig.residue_losses:
        lines.append("  (no residue stream data)")
    else:
        for r in sig.residue_losses:
            lines.append(f"  {r.vid} ({r.description}): "
                         f"moleflow={r.co2_moleflow:.4g}, "
                         f"share_of_feed_co2={r.share_of_feed_co2*100:.1f}%")

    lines.append("")
    lines.append("Candidate status:")
    if not sig.candidate_status:
        lines.append("  (no candidates)")
    else:
        for name in sorted(sig.candidate_status):
            lines.append(f"  {name}={sig.candidate_status[name]}")

    return "\n".join(lines)
