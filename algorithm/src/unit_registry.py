"""Registry mapping unit types to optimisation variable definitions, bounds and structure templates.

Responsibilities:
- Continuous variables (parameters the inner optimizer moves) and default bounds per unit type
- Structure template per unit type (the set of outlet port names)
- bounds_override on the unit data takes precedence over the defaults
- Robeson membrane model (10.1): permeance_CO2 is an optimisation variable and the
  selectivity is derived from the Robeson 2008 CO2/N2 upper bound (referred to a
  0.1 um membrane thickness)

Dependencies: none (Aspen-independent, domain-independent logic)
"""

from __future__ import annotations

from typing import Any

# =========================================================
# Robeson membrane model (10.1)
# =========================================================
#
# Unit conversion: the permeance unit of the Aspen GasPermModule is m3(STP)/(m2.h.bar).
# 1 GPU = 1e-6 cm3(STP)/(cm2.s.cmHg) ~= 2.70677e-3 m3(STP)/(m2.h.bar).
# The conversion factor is pinned so that it agrees exactly with the membrane
# parameters used so far in this project (permeance_CO2=2.70677 being the 1000 GPU
# of the first-generation MTR Polaris, and permeance_N2 being 1/50 of it = alpha 50).
# The 0.24% gap against the value derived from physical constants (2.70022e-3) comes
# from rounding in the cmHg conversion; continuity with the existing assets wins.
GPU_TO_ASPEN = 2.70677e-3

# Defaults for the Robeson 2008 CO2/N2 upper bound (Lee et al. 2018, Eq. 21):
#   permeance_CO2 [GPU] = k * alpha^(-n)  (referred to 0.1 um thickness)
#   <=> alpha = (k / Q[GPU])^(1/n)
# Overridable from membrane_model: in case.yaml (human-managed).
_ROBESON_DEFAULTS: dict[str, Any] = {
    "robeson_k_gpu": 3.0967e8,
    "robeson_n": 2.888,
    "gpu_to_aspen": GPU_TO_ASPEN,
    # Search range [GPU]. Covers Lee's sensitivity range 500-5000 and the
    # per-stage optimum 5986
    "permeance_bounds_gpu": [500.0, 6000.0],
    "tie": True,   # same membrane for every stage (one shared variable). False = independent per stage
}


def _mm_cfg(membrane_model: dict[str, Any] | None) -> dict[str, Any]:
    """Merge the membrane_model settings onto the defaults and return them."""
    return {**_ROBESON_DEFAULTS, **(membrane_model or {})}


def robeson_alpha(permeance_co2_aspen: float, membrane_model: dict[str, Any] | None = None) -> float:
    """Return the CO2/N2 selectivity alpha on the Robeson upper bound for a CO2 permeance (Aspen units).

    alpha = (k / Q[GPU])^(1/n). The upper bound itself is used as the frontier (the
    same anchor as Lee 2018; today's Polaris membrane at 1000 GPU / alpha 50 sits
    below the bound, where alpha(1000 GPU) ~= 80). permeance_N2 is then derived as
    permeance_CO2 / alpha.
    """
    cfg = _mm_cfg(membrane_model)
    q_gpu = float(permeance_co2_aspen) / float(cfg["gpu_to_aspen"])
    if q_gpu <= 0.0:
        raise ValueError(f"permeance_CO2 must be positive, got {permeance_co2_aspen!r}")
    alpha = (float(cfg["robeson_k_gpu"]) / q_gpu) ** (1.0 / float(cfg["robeson_n"]))
    return max(alpha, 1.0)  # guard: never allow alpha<1 (reverse selectivity), even out of range


def permeance_bounds_aspen(membrane_model: dict[str, Any] | None = None) -> list[float]:
    """Return the bounds of the permeance_CO2 variable in Aspen units (converting the GPU spec)."""
    cfg = _mm_cfg(membrane_model)
    lo_gpu, hi_gpu = cfg["permeance_bounds_gpu"]
    c = float(cfg["gpu_to_aspen"])
    return [float(lo_gpu) * c, float(hi_gpu) * c]


def is_tie_mode(membrane_model: dict[str, Any] | None) -> bool:
    """Whether tie mode is on (same membrane for every stage = one shared permeance variable)."""
    return bool(_mm_cfg(membrane_model).get("tie", True))


# Default bounds of the continuous variables (per unit type)
# (10.3): consistent with S2.6 of the prior work Lee et al., J. Membr. Sci. 563 (2018) 820-834.
#   - area: [1e5, 1.5e6] m^2 per stage (the feed is also scaled up to Lee's 500 Nm^3/s
#     equivalent; see feed.totflow in case.yaml. The cost model is linear, so $/tCO2 is
#     scale-invariant)
#   - p_permeate: vacuum pump suction pressure 0.1-1 bar (0.01 bar = 10 mbar is
#     industrially unrealistic. The upper limit is 0.99 to avoid zero driving force)
#   - COMP.outlet_pressure: in the blower campaign this is [1.1, 1.1] = fixed
#     (make_ga_variables drops lo==hi from the optimisation variables). It enforces the Merkel/MTR-type scenario in which every membrane
#     inlet is unified at 1.1 bar, so any COMP the agent adds later is also a blower with
#     no variable. Writing anything other than 1.1 into params is rejected by the
#     fixed-parameter guard in apply_ss.
#     For a Lee-consistent variable-compression campaign the value would be [1.0, 4.0].
UNIT_BOUNDS: dict[str, dict[str, list[float]]] = {
    "MEMB": {
        "area":       [100000.0, 1500000.0],
        "p_permeate": [0.1, 0.99],
    },
    "COMP": {
        "outlet_pressure": [1.1, 1.1],
    },
    # Expander (10.4): no optimisation variable (the outlet pressure is fixed by
    # params.outlet_pressure, default 1 bar). The agent places it as a structural part on a
    # high-pressure path (e.g. retentate after compression) to recover power.
    "EXP": {},
    # Cooler/heater (10.4): no optimisation variable
    # (params.temperature [degC]; params.pressure is the PRES spec of the Aspen Heater,
    # 0 meaning no pressure drop). Uses the builder's _create_heater. Placed as an intercooler that brings hot compressed gas back to the
    # membrane operating temperature (cooling-water cost is outside the model, consistent
    # with the decision to omit HX).
    "HEAT": {},
}

# Declaration of the continuous variables (used to assemble the optimisation variable names)
_UNIT_GA_VARS: dict[str, list[dict[str, str]]] = {
    "MEMB": [
        {"suffix": "area",   "param": "area"},
        {"suffix": "p_perm", "param": "p_permeate"},
    ],
    "COMP": [
        {"suffix": "pout", "param": "outlet_pressure"},
    ],
    "EXP": [],
    "HEAT": [],
}

# Structure templates (consulted when add_unit creates the outlet vertices)
# outlets is a list of port names. The order is deterministic (apply_ss numbers the new
# vertices in this order)
STRUCTURE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "MEMB": {"outlets": ["permeate", "retentate"]},
    "COMP": {"outlets": ["outlet"]},
    "EXP":  {"outlets": ["outlet"]},
    "HEAT": {"outlets": ["outlet"]},
}


def get_unit_type(unit_name: str) -> str | None:
    """Extract the type prefix from a unit name ("MEMB1" -> "MEMB").

    Returns None for a prefix that is not registered in UNIT_BOUNDS.
    """
    for prefix in UNIT_BOUNDS:
        if unit_name.startswith(prefix):
            return prefix
    return None


def make_ga_variables(
    unit_name: str,
    unit_data: dict[str, Any] | None = None,
    membrane_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the list of continuous optimisation variables belonging to a single unit.

    Parameters
    ----------
    unit_name : str
        Unit name (e.g. "MEMB1"). The type is identified by the prefix.
    unit_data : dict | None
        The units[name] entry of the SS. If it carries
        `bounds_override: {param_name: [lo, hi]}`, that takes precedence over the
        UNIT_BOUNDS default for the given param.
    membrane_model : dict | None
        The membrane_model: section of case.yaml (10.1). When given with tie=False
        (independent membrane per stage), a permeance_CO2 variable `{unit}_perm` is
        added to each MEMB. Under tie=True (same membrane for every stage) the shared
        variable is added once at the front by topology.continuous_variables instead
        (not here). None keeps the previous behaviour (permeance is a fixed value in
        params).

    Returns
    -------
    list[dict]
        Each element is {"name": str, "unit_param": [unit_name, param], "bounds": [lo, hi]}.
    """
    unit_type = get_unit_type(unit_name)
    if unit_type is None:
        return []

    overrides = (unit_data or {}).get("bounds_override", {}) or {}

    variables: list[dict[str, Any]] = []
    for spec in _UNIT_GA_VARS[unit_type]:
        param = spec["param"]
        bounds = overrides.get(param, UNIT_BOUNDS[unit_type][param])
        if bounds[0] == bounds[1]:
            # Fixed parameter: setting a bounds_override to lo==hi drops it
            # from the optimisation variables (the value is then carried by the fixed value in
            # units[name].params, so the seed must write the same value into params too).
            # This is the clean way to keep a zero-width dimension out of BO's
            # normalisation. Used for "no feed compression = pout fixed at 1.1 bar".
            continue
        variables.append({
            "name":       f"{unit_name}_{spec['suffix']}",
            "unit_param": [unit_name, param],
            "bounds":     list(bounds),
        })

    if unit_type == "MEMB" and membrane_model is not None and not is_tie_mode(membrane_model):
        variables.append({
            "name":       f"{unit_name}_perm",
            "unit_param": [unit_name, "permeance_CO2"],
            "bounds":     permeance_bounds_aspen(membrane_model),
        })

    return variables


def get_outlet_ports(unit_type: str) -> list[str]:
    """Return the list of outlet port names for a unit type (from STRUCTURE_TEMPLATES)."""
    template = STRUCTURE_TEMPLATES.get(unit_type)
    if template is None:
        raise KeyError(f"unknown unit_type: {unit_type!r}")
    return list(template["outlets"])
