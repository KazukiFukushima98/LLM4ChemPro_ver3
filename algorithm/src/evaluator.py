"""Abstract evaluation boundary (Evaluator Protocol), result types, and the cost objective (10.2).

Defines the Evaluator Protocol of ARCHITECTURE 5.2 together with the Metrics /
DetailedResult dataclasses. The implementation lives in simulator.AspenEvaluator.
Swapping in a surrogate later (FMQA/BOQA etc.) only requires injecting a different
implementation. Tests inject mocks.

Cost objective (10.2):
    The pure function cost_per_tco2, which combines Metrics + membrane areas +
    case (economics/feed) into an annualized capture cost [$/tCO2], lives here --
    right next to Metrics, as part of the evaluation boundary -- so that the
    optimizers (ga/bo) and run_iteration all use the same definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


BAD_VALUE: float = 1.0e6

# Defaults for the economics: section (Lee et al., J. Membr. Sci. 563 (2018) Table 1).
# Overridable via economics: in case.yaml (human-managed).
# Note 1: The heat-exchanger cost (Chx=300 $/m2) applies to the automatic coolers.
#     Negative WNET appears only with an expander (10.4).
# Note 2: CAPEX of pressure equipment is **C_unit x |WNET| (electric power as-is, not
#     divided by eta)**. Dividing electric power by eta as Lee Eq.16 literally reads
#     (C.W/eta) overshoots the paper's own values (C_cap in Fig.3/4) systematically by
#     +8 to 10%, as confirmed by reproducing all four designs. The W/eta in
#     Eq.16 converts isentropic work to actual power, and Aspen's WNET is already actual
#     power, so no further division is needed. Without eta and with HX omitted, the gap
#     to the published values stays around -2% for all four designs (~= the omitted HX).
ECONOMICS_DEFAULTS: dict[str, float] = {
    "membrane_cost": 50.0,          # $/m2 (module and skid included)
    "compressor_cost": 670.0,       # $/kW
    "vacuum_pump_cost": 1341.0,     # $/kW
    "expander_cost": 500.0,         # $/kW (expander, 10.4)
    "hx_cost": 300.0,               # $/m2 (cooler; Lee Eq.16 C_hx)
    "installation_factor": 1.6,     # f_in (multiplies total CAPEX)
    "capital_charge_rate": 0.2,     # /y (annual capital charge rate)
    "electricity_cost": 0.04,       # $/kWh
    "operating_hours": 7446.0,      # h/y (85% availability)
    "penalty_weight": 1000.0,       # shortfall^2 coefficient for the cost objective (Lee's r)
}


def feed_co2_t_per_h(feed: dict[str, Any]) -> float:
    """Compute the CO2 mass flow [t/h] in the feed from case.yaml.feed.

    **TOTFLOW is interpreted as a molar flow [kmol/h]**: case.yaml writes flowbase: MASS,
    but measurement established that TOTFLOW actually acts as a molar flow
    (totflow=2.44e6 -> total molar flow at V0 of 2.44e6 kmol/h). Hence
    CO2 mass flow = totflow[kmol/h] x co2_frac x MW_CO2 / 1000.

    The guard is kept as a detector for deviations from the configuration combination that
    is known to work (changing the written columns may change the actual behaviour, in
    which case this conversion needs to be re-verified).
    """
    if str(feed.get("flowbase", "MASS")).upper() != "MASS" or \
       str(feed.get("basis", "MOLE-FRAC")).upper() != "MOLE-FRAC":
        raise ValueError(
            "feed_co2_t_per_h supports only FLOWBASE=MASS + BASIS=MOLE-FRAC "
            "(measured: TOTFLOW is molar) "
            f"(got flowbase={feed.get('flowbase')!r}, basis={feed.get('basis')!r})"
        )
    x = float(feed["co2_frac"])            # CO2 mole fraction
    mw_co2 = 44.0                          # matches the spec_e conversion in simulator (44.0)
    return float(feed["totflow"]) * x * mw_co2 / 1000.0   # kmol/h -> t/h


def _pressure_unit_cost_per_kw(block_name: str, wnet: float, econ: dict[str, float]) -> float:
    """Look up the unit cost [$/kW] from the energy_breakdown block name and the sign of WNET.

    VP{n} = vacuum pump; WNET<0 = expander (turbine, power recovery); anything else
    (COMP etc.) = compressor.
    """
    if wnet < 0.0:
        return float(econ["expander_cost"])
    if block_name.startswith("VP"):
        return float(econ["vacuum_pump_cost"])
    return float(econ["compressor_cost"])


def cost_per_tco2(
    metrics: Metrics,
    membrane_areas: dict[str, float],
    case: dict[str, Any],
) -> float:
    """Compose the annualized CO2 capture cost [$/tCO2] (Lee 2018 Eq.15/16, incl. the HX term).

        cost = (capital_charge * f_in * C_TCC) / (M_CO2 * t_op) + E * Ce
        C_TCC = sum_memb Cm*A + sum_blk C_unit(blk) * |WNET_blk| + Chx * A_hx
        M_CO2 = recovery x feed CO2 mass flow [t/h]
        E     = specific energy [kWh/tCO2] (equivalent to OPEX/tCO2 = E * Ce)

    WNET is treated as electric power itself and is not divided by eta (ECONOMICS_DEFAULTS
    note 2; agreement with the published C_cap was confirmed for the four designs of Lee
    Fig.3/4, the -2% gap being the omitted HX).
    A_hx is computed by the simulator from the cooler blocks via Lee Eq.5/6 (U=132.5 W/m2K,
    cooling water 20->25 degC, counter-current LMTD) and carried in Metrics.hx_area_m2
    .

    Parameters
    ----------
    metrics        : valid Metrics (a bad one yields BAD_VALUE)
    membrane_areas : {unit_name: area_m2}. Pass **only the membranes present in the
                     concrete topology** (pruned membrane areas must not enter CAPEX)
    case           : contents of case.yaml (economics / feed are read)

    Returns
    -------
    float : $/tCO2. BAD_VALUE if metrics is bad or recovery is zero.
    """
    if metrics.specific_energy >= BAD_VALUE:
        return BAD_VALUE
    econ = {**ECONOMICS_DEFAULTS, **(case.get("economics") or {})}

    m_co2 = metrics.recovery * feed_co2_t_per_h(case["feed"])   # t/h
    if m_co2 <= 0.0:
        return BAD_VALUE

    c_tcc = sum(float(econ["membrane_cost"]) * float(a) for a in membrane_areas.values())
    c_tcc += sum(
        _pressure_unit_cost_per_kw(blk, w, econ) * abs(float(w))
        for blk, w in metrics.energy_breakdown.items()
    )
    c_tcc += float(econ["hx_cost"]) * float(getattr(metrics, "hx_area_m2", 0.0))

    annual_capex = float(econ["capital_charge_rate"]) * float(econ["installation_factor"]) * c_tcc
    capex_per_t  = annual_capex / (m_co2 * float(econ["operating_hours"]))
    opex_per_t   = metrics.specific_energy * float(econ["electricity_cost"])
    return capex_per_t + opex_per_t


def membrane_areas_from_x(
    x: list[float],
    cont_vars: list[dict[str, Any]],
    topology: dict[str, Any],
) -> dict[str, float]:
    """Extract membrane areas {unit: m2} from an x in concrete-topology dimensions (for cost_per_tco2).

    x must be in the same "concrete topology dimensions" as the one passed to the evaluator
    (i.e. after x_for_topology), and cont_vars must be in the same order as
    continuous_variables(topology, membrane_model).
    Membranes whose area variable is absent from x (should not happen; defensive) fall back
    to the value in units.params.
    """
    areas: dict[str, float] = {}
    for uname, udef in topology.get("units", {}).items():
        if udef.get("type") == "MEMB" or uname.startswith("MEMB"):
            p_area = (udef.get("params") or {}).get("area")
            if p_area is not None:
                areas[uname] = float(p_area)
    for val, cv in zip(x, cont_vars):
        uname, param = cv["unit_param"]
        if param == "area" and uname in topology.get("units", {}):
            areas[uname] = float(val)
    return areas


@dataclass
class Metrics:
    """Minimal metric set needed for fitness evaluation inside the inner optimisation loop (ARCHITECTURE 5.2)."""

    specific_energy: float                        # kWh/tCO2
    purity: float                                 # CO2 mol fraction [0, 1]
    recovery: float                               # CO2 recovery [0, 1]
    energy_breakdown: dict[str, float] = field(default_factory=dict)  # block -> WNET [kW]
    hx_area_m2: float = 0.0                       # total heat-transfer area of the auto coolers [m2]
                                                  # (Lee Eq.5/6; HX cost)

    @classmethod
    def bad(cls) -> "Metrics":
        """Return the sentinel used on Aspen non-convergence, crash, or build failure."""
        return cls(specific_energy=BAD_VALUE, purity=0.0, recovery=0.0)


@dataclass
class DetailedResult:
    """Detailed extraction for the single best solution (the source of results.json)."""

    metrics: Metrics
    stream_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {"V0": {"CO2_molfrac": float, "CO2_moleflow": float | None, "description": str}, ...}


@runtime_checkable
class Evaluator(Protocol):
    """Abstract evaluation boundary (ARCHITECTURE 5.2).

    The inner optimizer invokes evaluation through this boundary. topology / ga.py / bo.py see nothing but
    this Protocol.
    """

    def evaluate_topology(
        self,
        topology: dict[str, Any],
        x_list: list[list[float]],
    ) -> list[Metrics]:
        """Build a given topology once, then evaluate x_list in order.

        Parameters
        ----------
        topology : concrete topology {vertices, arcs, units}
        x_list   : list of continuous-variable vectors.
                   Each x follows the order returned by topology.continuous_variables(topology).

        Returns
        -------
        A Metrics list of the same length as x_list.
        Aspen non-convergence and crashes are absorbed as Metrics.bad().
        """
        ...

    def evaluate_detailed(
        self,
        topology: dict[str, Any],
        x: list[float],
    ) -> DetailedResult:
        """Detailed extraction for the single best solution, including stream_results
        (the CO2 information of each vertex).

        On build or run failure, returns DetailedResult(metrics=Metrics.bad()).
        """
        ...
