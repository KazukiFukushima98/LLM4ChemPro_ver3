"""Aspen execution, crash detection and timeout handling + AspenEvaluator.

run_aspen_with_timeout:
    A faithful port from the old LLM4ChemPro/algorithm/src/simulator.py. Logic unchanged.

AspenEvaluator (phase 4a):
    The Aspen-facing parts of evaluate_group / get_detailed_results in the old
    run_iteration.py, ported here. The duplicated implementations (_safe / _apply_feed)
    are consolidated into one place.
"""

import glob
import math
import os
import sys
import time
import pythoncom

sys.path.insert(0, os.path.dirname(__file__))

import aspen_watchdog  # noqa: E402
from aspen_builder import build_aspen_from_epnt, kill_aspen_image, set_continuous_variables  # noqa: E402
from evaluator import BAD_VALUE, DetailedResult, Metrics                   # noqa: E402
from topology import auto_vps as _auto_vps, continuous_variables            # noqa: E402
from unit_registry import robeson_alpha                                     # noqa: E402


class AspenCrashError(RuntimeError):
    """Raised when Aspen crashes (detected via new .dmp file in dmp_dir)."""


# Constants for sizing the HX (automatic cooler) area (Lee 2018 section 2.3)
HX_U_W_M2K = 132.5     # overall heat transfer coefficient [W/m2K] (literature median for gas-cooling water)
HX_CW_IN_C = 20.0      # cooling water inlet [degC]
HX_CW_OUT_C = 25.0     # cooling water outlet [degC]
HX_GAS_TOUT_C = 35.0   # gas outlet [degC] (same as AUTO_COOLER_TEMP_C = membrane operating temperature)
# QCALC unit conversion: in this .apw the unit set gives power (WNET) in kW but heat
# flow (QCALC) in cal/s. Verified on the real model: VP1's QCALC/WNET is
# exactly 4.1868, i.e. the IT calorie factor (a cross-check exploiting the physics that
# a VP does adiabatic compression followed by cooling all the way back to 35 degC, so
# duty ~= work).
QCALC_CAL_S_TO_KW = 4.1868e-3


def hx_area_m2_from_duty(q_kw: float | None, t_in_c: float | None) -> float:
    """Heat transfer area of a single cooler [m2] (Lee Eq.5/6, counter-current LMTD).

    q_kw   : cooler duty [kW] (Aspen QCALC; negative for cooling)
    t_in_c : gas inlet temperature [degC] (temperature of the intermediate stream
             upstream of the cooler)

    Returns 0 for cases that are not valid cooling (duty >= 0 = heating, gas inlet at or
    below 35 degC, cooling water outlet at or below 25 degC), e.g. when the blower
    outlet is below 35 degC.
    """
    if q_kw is None or t_in_c is None:
        return 0.0
    if q_kw >= 0.0 or t_in_c <= HX_GAS_TOUT_C:
        return 0.0
    dt1 = t_in_c - HX_CW_OUT_C            # hot end: gas inlet - cooling water outlet
    dt2 = HX_GAS_TOUT_C - HX_CW_IN_C      # cold end: 35 - 20 = 15
    if dt1 <= 0.0:
        return 0.0
    lmtd = dt2 if abs(dt1 - dt2) < 1e-9 else (dt1 - dt2) / math.log(dt1 / dt2)
    return abs(q_kw) * 1000.0 / (HX_U_W_M2K * lmtd)   # kW -> W


def build_unit_params(
    x: list[float],
    cont_vars: list[dict],
    topology: dict,
    membrane_model: dict | None = None,
) -> dict[str, dict[str, float]]:
    """Assemble the unit_params for set_continuous_variables from x (concrete-topology dimension).

    - An entry whose unit name ends with "*" (the tie-shared permeance "MEMB*") is
      expanded to the same value for every unit in the topology carrying that prefix
    - If membrane_model (10.1) is given, units that have permeance_CO2 get
      permeance_N2 = permeance_CO2 / alpha derived from the Robeson upper bound

    A pure function independent of Aspen (it touches no COM, so it is unit-testable).
    """
    unit_params: dict[str, dict[str, float]] = {}
    for val, cv in zip(x, cont_vars):
        uname, param = cv["unit_param"]
        if uname.endswith("*"):
            prefix = uname[:-1]
            targets = [u for u in topology.get("units", {}) if u.startswith(prefix)]
        else:
            targets = [uname]
        for u in targets:
            unit_params.setdefault(u, {})[param] = float(val)

    if membrane_model is not None:
        for params in unit_params.values():
            if "permeance_CO2" in params:
                alpha = robeson_alpha(params["permeance_CO2"], membrane_model)
                params["permeance_N2"] = params["permeance_CO2"] / alpha

    return unit_params


MAX_REBUILDS_PER_GROUP = 3  # max rebuilds allowed within one group (prevents infinite loops)


def run_aspen_with_timeout(aspen, timeout=120, dmp_dir=None):
    """Run Aspen and raise TimeoutError or AspenCrashError on failure.

    Reports loop progress to the watchdog via beat(). If a COM call blocks and the
    watchdog thread kills Aspen, the blocked call returns as an exception from the RPC
    disconnect (-2147023xx) -> it is converted to AspenCrashError here, feeding the
    "rebuild once" path of the caller (evaluate_topology).
    """
    existing_dmps = set(glob.glob(os.path.join(dmp_dir, "*.dmp"))) if dmp_dir else set()

    aspen_watchdog.beat()
    try:
        aspen.Reinit()
        aspen.Run2(1)
    except Exception as e:
        # If starting the run itself fails, the engine state cannot be trusted.
        # Rather than "mark just this x bad and continue", convert to AspenCrashError so
        # the caller's rebuild path takes over (this keeps a broken engine from wiping
        # out the rest of the group).
        raise AspenCrashError(f"Reinit/Run2 failed (engine unusable): {e}") from e

    start = time.time()
    try:
        while True:
            aspen_watchdog.beat()            # heartbeat (emitted right before blocking)
            pythoncom.PumpWaitingMessages()

            if aspen.Engine.IsRunning != 1:
                break

            if dmp_dir:
                current_dmps = set(glob.glob(os.path.join(dmp_dir, "*.dmp")))
                if current_dmps - existing_dmps:
                    raise AspenCrashError("Aspen crashed (new .dmp file detected)")

            if time.time() - start > timeout:
                try:
                    aspen.Engine.Stop()
                except Exception:
                    pass
                # Engine.Stop alone does not work on a hang, so force a kill
                # (with a timeout; os.system would itself get stuck here on an unkillable Aspen)
                kill_aspen_image()
                time.sleep(3)
                raise TimeoutError(f"Aspen run timed out after {timeout}s")

            time.sleep(0.5)
    except AspenCrashError:
        raise
    except TimeoutError:
        raise
    except Exception as e:
        # A watchdog kill / crash that severs COM surfaces as an RPC disconnect
        if "-2147023" in str(e):
            raise AspenCrashError(f"RPC disconnect (Aspen killed/crashed): {e}")
        raise


class AspenEvaluator:
    """Evaluation of a concrete topology using Aspen Plus (implements the Evaluator Protocol).

    The duplicated implementations in the source (old run_iteration.py) are consolidated here:
      _safe       <- :179-184 (inside evaluate_group) + :306-311 (inside get_detailed_results)
      _apply_feed <- :168-172 (inside _build_aspen)   + :294-297 (inside get_detailed_results)

    evaluate_topology <- :151-239 (the Aspen-facing part of evaluate_group)
    evaluate_detailed <- :281-356 (get_detailed_results)
    """

    def __init__(self, case: dict, aspen_file: str, dmp_dir: str) -> None:
        """
        Parameters
        ----------
        case       : contents of case.yaml (dict). Includes feed / optimization_targets.
        aspen_file : absolute path to Yaspen.apw.
        dmp_dir    : directory where .dmp files are written (used for crash detection).
        """
        self._case = case
        self._aspen_file = aspen_file
        self._dmp_dir = dmp_dir

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _safe(self, aspen, path: str, default=None):
        """Safely read a value from the Aspen tree. Returns default on a missing node or exception.

        Consolidates the duplication between the old evaluate_group:179-184 and
        get_detailed_results:306-311.
        """
        try:
            n = aspen.Tree.FindNode(path)
            return float(n.value) if n and n.value is not None else default
        except Exception:
            return default

    def _apply_feed(self, aspen) -> None:
        """Write the case.yaml feed specification to stream V0 (7 items).

        The builder carries no feed defaults (ARCHITECTURE section 9), so the complete
        specification (FLOWBASE / TOTFLOW / composition / BASIS / TEMP / PRES) is set here
        in one place. BASIS / TEMP / PRES must be written explicitly: leaving them unset
        causes BAD_VALUE.
        """
        feed = self._case["feed"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\BASIS\MIXED").value    = feed["basis"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\TEMP\MIXED").value     = feed["temp"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\PRES\MIXED").value     = feed["pressure"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\FLOWBASE\MIXED").value = feed["flowbase"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\TOTFLOW\MIXED").value  = feed["totflow"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\FLOW\MIXED\CARBO-01").value = feed["co2_frac"]
        aspen.Tree.FindNode(r"\Data\Streams\V0\Input\FLOW\MIXED\NITRO-01").value = 1.0 - feed["co2_frac"]

    def _build(self, topology: dict) -> tuple:
        """Build Aspen and return (aspen, energy_blocks, coolers). Returns (None, None, None) on failure.

        Corresponds to the old _build_aspen function (run_iteration.py:155-173).
        build_aspen_from_epnt already does a taskkill internally (aspen_builder.py:68), so
        no taskkill is needed beforehand; it is called only to clean up after a failure.
        """
        existing_dmps = set(glob.glob(os.path.join(self._dmp_dir, "*.dmp")))
        try:
            aspen, _ = build_aspen_from_epnt(topology, self._aspen_file)
            new_dmps = set(glob.glob(os.path.join(self._dmp_dir, "*.dmp"))) - existing_dmps
            if new_dmps:
                print("    Aspen crash during build (.dmp detected)")
                kill_aspen_image()
                return None, None, None
            self._apply_feed(aspen)   # <- inside the try (also catches a COM write hang)
        except Exception as e:
            print(f"    Aspen build failed: {e}")
            kill_aspen_image()
            return None, None, None

        # energy_blocks = auto-VP names + explicit COMP/EXP unit names
        # (an EXP = expander has negative WNET, i.e. it counts as recovered power in the
        # total. 10.4)
        vp_map = _auto_vps(topology)
        energy_blocks: list[str] = list(vp_map.values())
        for uname, udef in topology["units"].items():
            if udef.get("type") in ("COMP", "EXP") and uname not in energy_blocks:
                energy_blocks.append(uname)

        # coolers = automatic coolers as (block, inlet intermediate stream). Used to size
        # the area for the HX cost (Lee Eq.5/6). Builder naming convention:
        # VP{n}->HXV{n}/VPO{n}, COMP{n}->HXC{n}/HCI{n}. An EXP has no cooler (expansion
        # lowers the temperature).
        coolers: list[tuple[str, str]] = []
        for vp in vp_map.values():
            n = vp[2:]                      # "VP3" -> "3"
            coolers.append((f"HXV{n}", f"VPO{n}"))
        for uname, udef in topology["units"].items():
            if udef.get("type") == "COMP":
                n = "".join(ch for ch in uname if ch.isdigit())
                coolers.append((f"HXC{n}", f"HCI{n}"))

        return aspen, energy_blocks, coolers

    def _product_vid(self, topology: dict) -> str | None:
        """Return the ID of the product-role vertex (None if there is none)."""
        return next(
            (v for v, d in topology["vertices"].items() if d.get("role") == "product"),
            None,
        )

    def _extract_metrics(
        self, aspen, product_vid: str, energy_blocks: list[str],
        coolers: list[tuple[str, str]] | None = None,
    ) -> Metrics:
        """Extract Metrics from a converged Aspen run."""
        purity  = self._safe(aspen, rf"\Data\Streams\{product_vid}\Output\MOLEFRAC\MIXED\CARBO-01", 0.0)
        prod_mf = self._safe(aspen, rf"\Data\Streams\{product_vid}\Output\MOLEFLOW\MIXED\CARBO-01", 0.0)
        v0_mf   = self._safe(aspen, r"\Data\Streams\V0\Output\MOLEFLOW\MIXED\CARBO-01", 0.0)

        energy_bd: dict[str, float] = {}
        total_kw = 0.0
        for blk in energy_blocks:
            w = self._safe(aspen, rf"\Data\Blocks\{blk}\Output\WNET", 0.0)
            if w:
                energy_bd[blk] = w
                total_kw += w

        # Total heat transfer area of the automatic coolers (Lee Eq.5/6, for the HX cost)
        hx_area = 0.0
        for blk, inlet in (coolers or []):
            q_cal_s = self._safe(aspen, rf"\Data\Blocks\{blk}\Output\QCALC")
            t_in = self._safe(aspen, rf"\Data\Streams\{inlet}\Output\TEMP_OUT\MIXED")
            q_kw = None if q_cal_s is None else q_cal_s * QCALC_CAL_S_TO_KW
            hx_area += hx_area_m2_from_duty(q_kw, t_in)

        if not v0_mf or v0_mf <= 0 or not prod_mf or prod_mf <= 0:
            return Metrics.bad()

        recovery = prod_mf / v0_mf

        # Mass-balance guard: even when a recycle tear reports
        # "converged" under headless COM, it can settle on a non-physical point
        # (product CO2 > feed CO2, i.e. recovery > 1). A recovery > 1 violates the mass
        # balance, so this cross-check catches it and rejects the point as BAD (keeping
        # the optimizer from chasing garbage solutions as the best). recovery_physical_max is
        # tunable in case.yaml (default 1.02).
        recovery_max = float(self._case.get("recovery_physical_max", 1.02))
        if recovery > recovery_max:
            print(
                f"    mass-balance guard: recovery={recovery:.4f} > {recovery_max} "
                f"(prod_mf={prod_mf:.4g}, v0_mf={v0_mf:.4g}) -> BAD (non-physical, mass-balance violation)"
            )
            return Metrics.bad()

        # Energy cross-check guard (same shape as the mass-balance guard): energy_blocks
        # (VP/COMP) exist, yet the WNET total is <= 0 = every node read failed (naming
        # mismatch, or values missed because the run did not converge).
        # Letting this through yields a "perfect" solution with zero specific energy that
        # the optimizer would then chase as a false best. Zero is never physically legitimate:
        # an auto-VP always does compression work from p_permeate (<=0.99 bar) to 1 bar.
        if energy_blocks and total_kw <= 0:
            print(
                f"    energy guard: blocks={energy_blocks} but total WNET={total_kw:.4g} "
                f"-> BAD (every power reading failed = reject the false zero-energy solution)"
            )
            return Metrics.bad()

        spec_e   = total_kw / (prod_mf * 44.0 / 1000.0) if prod_mf > 0 else BAD_VALUE

        return Metrics(
            specific_energy=spec_e,
            purity=purity,
            recovery=recovery,
            energy_breakdown=energy_bd,
            hx_area_m2=hx_area,
        )

    # ------------------------------------------------------------------
    # Evaluator Protocol
    # ------------------------------------------------------------------

    def evaluate_topology(
        self,
        topology: dict,
        x_list: list[list[float]],
        on_result=None,
    ) -> list[Metrics]:
        """Build Aspen once and evaluate x_list in order (the minimal phase-4a form).

        A port of the Aspen-facing part of the old evaluate_group (run_iteration.py:151-239).
        On a crash it retries once (as in the old implementation). The timeout is
        case.yaml.aspen_timeout_eval (default 60s).

        Parameters
        ----------
        on_result : Callable[[int, Metrics], None] | None
            Callback invoked with `(index, metrics)` each time a result is finalized
            (backward-compatible; default None). aspen_worker (the child process) uses it
            to stream results one by one to stdout, and the parent (SubprocessEvaluator)
            detects a stall from the time since the last message. Even if it wedges
            midway, the parent keeps the x values already processed.
            Every result is finalized through emit(), so never call append/extend directly.
        """
        with aspen_watchdog.armed():
            membrane_model = self._case.get("membrane_model")
            cont_vars   = continuous_variables(topology, membrane_model)
            product_vid = self._product_vid(topology)
            results: list[Metrics] = []

            def emit(m: Metrics) -> None:
                """Finalize one result. Consolidates the append and the callback in one place."""
                results.append(m)
                if on_result is not None:
                    on_result(len(results) - 1, m)

            def emit_bad_rest() -> None:
                """Fill every remaining (unfinalized) x with bad."""
                for _ in range(len(x_list) - len(results)):
                    emit(Metrics.bad())

            if product_vid is None:
                emit_bad_rest()
                return results

            aspen, energy_blocks, coolers = self._build(topology)
            if aspen is None:
                emit_bad_rest()
                return results

            timeout_eval = int(self._case.get("aspen_timeout_eval", 60))
            rebuilds = 0

            for x in x_list:
                try:
                    unit_params = build_unit_params(x, cont_vars, topology, membrane_model)
                    set_continuous_variables(aspen, unit_params)
                    run_aspen_with_timeout(aspen, timeout=timeout_eval, dmp_dir=self._dmp_dir)
                    emit(self._extract_metrics(aspen, product_vid, energy_blocks, coolers))
                    continue
                except (AspenCrashError, TimeoutError):
                    pass  # -> falls through to the rebuild handling below
                except Exception as e:
                    if "-2147023" not in str(e):
                        # An unexpected non-COM-disconnect exception -> mark only this x bad
                        # and continue (Aspen is assumed to still be alive)
                        print(f"    eval error (non-COM, skipped): {e}")
                        emit(Metrics.bad())
                        continue
                    # COM disconnect (from a kill/crash) -> falls through to the rebuild handling below

                # Reaching here = crash / COM disconnect. Mark the failed x bad, rebuild, move on
                print(f"    Aspen crash/COM-disconnect (rebuilds={rebuilds})")
                emit(Metrics.bad())
                rebuilds += 1
                if rebuilds > MAX_REBUILDS_PER_GROUP:
                    print(f"    rebuild budget exhausted ({MAX_REBUILDS_PER_GROUP}) -> aborting the group")
                    emit_bad_rest()
                    break
                kill_aspen_image()
                aspen, energy_blocks, coolers = self._build(topology)
                if aspen is None:
                    emit_bad_rest()
                    break

            return results

    def evaluate_detailed(
        self,
        topology: dict,
        x: list[float],
    ) -> DetailedResult:
        """Detailed extraction for the single best solution. Also returns stream_results (CO2 data per vertex).

        A port of the Aspen-facing part of the old get_detailed_results (run_iteration.py:281-356).
        The timeout is case.yaml.aspen_timeout_detail (default 120s).
        On a build or run failure it returns DetailedResult(metrics=Metrics.bad()).
        """
        with aspen_watchdog.armed():
            membrane_model = self._case.get("membrane_model")
            cont_vars   = continuous_variables(topology, membrane_model)
            product_vid = self._product_vid(topology)
            if product_vid is None:
                return DetailedResult(metrics=Metrics.bad())

            aspen, energy_blocks, coolers = self._build(topology)
            if aspen is None:
                return DetailedResult(metrics=Metrics.bad())

            timeout_detail = int(self._case.get("aspen_timeout_detail", 120))
            try:
                unit_params = build_unit_params(x, cont_vars, topology, membrane_model)
                set_continuous_variables(aspen, unit_params)   # <- inside the try (also catches a COM write hang)
                run_aspen_with_timeout(aspen, timeout=timeout_detail, dmp_dir=self._dmp_dir)
            except Exception as e:
                print(f"    evaluate_detailed: run failed: {e}")
                return DetailedResult(metrics=Metrics.bad())

            # Iterates by vertex ID.
            # pressure_bar is for acceptance checking: it is recorded so that
            # the blower campaign's "all membrane inlets at 1.1 bar" can be confirmed
            # against measured pressures after a run (record-only).
            stream_results: dict = {}
            for vid, vdef in topology["vertices"].items():
                co2_frac = self._safe(aspen, rf"\Data\Streams\{vid}\Output\MOLEFRAC\MIXED\CARBO-01")
                co2_mf   = self._safe(aspen, rf"\Data\Streams\{vid}\Output\MOLEFLOW\MIXED\CARBO-01")
                pres     = self._safe(aspen, rf"\Data\Streams\{vid}\Output\PRES_OUT\MIXED")
                if co2_frac is not None:
                    stream_results[vid] = {
                        "CO2_molfrac":  co2_frac,
                        "CO2_moleflow": co2_mf,
                        "pressure_bar": pres,
                        "description":  vdef.get("label", vid),
                    }

            metrics = self._extract_metrics(aspen, product_vid, energy_blocks, coolers)
            return DetailedResult(metrics=metrics, stream_results=stream_results)
