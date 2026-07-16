"""Dynamically build an Aspen model from a concrete topology.

A faithful port of the old LLM4ChemPro/algorithm/src/aspen_builder.py.
Changes are limited to the three points defined in ARCHITECTURE 3.2 / 3.4 / 4 / 10:

  (1) Input interface
      old: (adj_matrix, arc_definitions, unit_params, aspen_file)
      new: (topology, aspen_file). topology is the 3-dict concrete topology
      {vertices, arcs, units}. Stable string IDs (e.g. V7) are used directly as Aspen
      stream names instead of integer indices. In the auxiliary names
      VS{i}T{j} / MIXV{j} / DV{i} / VPI{n} / VP{n}, {i}/{j}/{n} are the numeric parts
      of the vid / unit name.

  (2) Sink vertices added as Mixer rule (3)
      The old _find_mixer_vertices had rules (1)(2) only. In ver2, sinks
      (role=product/residue) are turned into Mixers so that they carry streams, which
      fixes the purity/recovery measurement points on the sinks (ARCHITECTURE 3.4).
      The rule is centralized in topology.mixer_vertices, so we delegate to it and
      _find_mixer_vertices is removed here (avoiding a duplicate definition).

  (3) Removal of the feed default
      The old _configure_feed carried a DAC default (420 ppm, MOLE-FRAC, etc.); it is
      removed. The builder holds no feed default and only ensures in Step 2 that the
      feed stream exists. Composition, flow rate and basis
      (FLOWBASE/TOTFLOW/CARBO-01/NITRO-01) are overridden by AspenEvaluator (phase 4)
      from case.yaml (ARCHITECTURE 10).

Everything else (membrane block creation, auto-VP insertion, naming rules, port
connection logic) follows the old implementation.

Stream naming:
  V{i}      : representative stream for vertex (= vertex ID such as "V7")
  VS{i}T{j} : intermediate stream entering a Mixer vertex ({i}/{j} are vid numeric parts)
  DV{i}     : discard stream for FSplit ({i} is vid numeric part)
  MIXV{j}   : Mixer block name ({j} is vid numeric part)
  MEMB{n}   : membrane block (unit name as-is)
  VP{n}/VPI{n} : auto-VP block and its inlet stream (n matches MEMB{n})
  VPO{n}/HXV{n}: intermediate stream at the auto-VP outlet and its auto-cooler (35 degC, ver3 12.4)
  HCI{n}/HXC{n}: intermediate stream at the COMP{n} outlet and its auto-cooler (same)

NOTE: Aspen does not allow underscores in block or stream names.
"""

import os
import subprocess
import sys
import win32com.client as win32

sys.path.insert(0, os.path.dirname(__file__))
from topology import mixer_vertices as _topology_mixer_vertices  # noqa: E402


def kill_aspen_image(timeout: float = 20) -> None:
    """Run "taskkill /f /im AspenPlus.exe" with a timeout (cleanup on the child-process side).

    os.system has no timeout and can block the caller indefinitely on a wedged Aspen that
    cannot be killed (the same failure mode as the 85-minute hang in run13; already fixed
    on the subprocess_evaluator side). All Aspen cleanup inside a child process (worker)
    goes through this function.
    Failures and timeouts are swallowed: the top priority is not to stall the evaluation
    pipeline over a failed cleanup, and any leftover Aspen is reclaimed by the taskkill at
    the next build startup or by the parent's wedge handling.
    """
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "AspenPlus.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except Exception:
        pass


def build_aspen_from_epnt(topology, aspen_file):
    """
    Dynamically build an Aspen model from a concrete topology.

    Parameters
    ----------
    topology : dict
        Concrete topology with keys:
          - vertices: {"V0": {"role": ..., "label": ...}, ...}
          - arcs:     {(vid_from, vid_to): {"type": ..., "unit": ..., ...}, ...}
          - units:    {"MEMB1": {"type": ..., "inlet": ..., "outlets": {...}, "params": {...}}, ...}
        Vertex IDs are used directly as Aspen stream names (V7, etc.).
    aspen_file : str
        Path to the Aspen archive or APW file.

    Returns
    -------
    aspen : win32com object
    auto_vps : list[str]
        Names of vacuum pump blocks that were automatically added (e.g. ["VP1", "VP3"]).
        VP{n} (Compr, outlet=1 bar) is auto-inserted on the permeate side of each MEMB{n}.
        WNET is included in the energy sum.
    """
    kill_aspen_image()
    aspen = win32.Dispatch('Apwn.Document')
    aspen.InitFromArchive2(os.path.abspath(aspen_file))
    aspen.Visible = 0
    aspen.SuppressDialogs = 1

    vertices = topology["vertices"]
    arcs = topology["arcs"]
    units_dict = topology["units"]
    unit_params = {name: udef.get("params", {}) for name, udef in units_dict.items()}

    block_node  = aspen.Tree.FindNode(r'\Data\Blocks')
    stream_node = aspen.Tree.FindNode(r'\Data\Streams')

    # --------------------------------------------------
    # Step 0: Identify Mixer vertices
    #   (1) src vertex of membrane_permeate/retentate arcs
    #   (2) any vertex with in-degree >= 2
    #   (3) sink (product/residue) vertices  <- added in ver2 (measurement points as streams)
    # Rule is centralized in topology.mixer_vertices; delegate to it.
    # --------------------------------------------------
    mixer_set = _topology_mixer_vertices(topology)

    # --------------------------------------------------
    # Step 1: Determine output stream names for each arc and collect Mixer input lists
    #
    #   Output stream for arc (i→j):
    #     j is a Mixer vertex → VS{_num(i)}T{_num(j)}  (intermediate stream going into the Mixer)
    #     j is a normal vertex → j  (vertex ID is the stream name)
    #
    #   mixer_inputs[j] = list of stream names to connect to MIXV{_num(j)}
    # --------------------------------------------------
    mixer_inputs = {j: [] for j in mixer_set}

    for (i, j), arc_def in arcs.items():
        if j not in mixer_set:
            continue

        if arc_def.get("unit"):
            # Unit arc (membrane included). Outlet name is VS{_num(i)}T{_num(j)}.
            # Port connection is done on the unit side (_create_membrane / _create_compressor
            # etc.), but stream creation (Step 2) and registration to the Mixer F(IN)
            # (Step 3) must be queued here.
            # Membranes falling through to pass here caused the 3-stage build failure
            # (VS{i}T{j} was never created).
            mixer_inputs[j].append(f"VS{_num(i)}T{_num(j)}")
        else:
            # No-op arc (feed etc.) → feed the upstream vertex stream directly into Mixer
            mixer_inputs[j].append(i)

    # --------------------------------------------------
    # Step 2: Create streams
    #   - representative stream for each vertex (vid string), excluding terminal vertices
    #   - intermediate streams VS{...} going into Mixer vertices
    #
    #   Terminal vertex: no outgoing arcs AND not a Mixer vertex AND
    #                    no unit block on any incoming arc (only no-op arcs)
    #   → Would become an unconnected stream in Aspen, so skip creation.
    #   In ver2, sinks are always Mixer vertices (rule 3), so they are NOT terminal —
    #   they get representative streams as measurement points (ARCHITECTURE 3.4).
    # --------------------------------------------------
    terminal_vertices = _find_terminal_vertices(vertices, arcs, mixer_set)

    for vid in vertices:
        if vid not in terminal_vertices:
            stream_node.Elements.Add(vid)

    for j in mixer_set:
        for s in mixer_inputs[j]:
            if s.startswith("VS"):
                stream_node.Elements.Add(s)

    # --------------------------------------------------
    # Step 3: Create and connect Mixer blocks
    #   F(IN)  = each stream in mixer_inputs[j]
    #   P(OUT) = j  (vid string = representative stream of vertex j)
    # --------------------------------------------------
    for j in mixer_set:
        mixer_name = f"MIXV{_num(j)}"
        block_node.Elements.Add(f"{mixer_name}!Mixer")
        aspen.Tree.FindNode(rf"\Data\Blocks\{mixer_name}\Input\T_EST").value = 25
        # ver3 12.4: PRES=0 means "follow the minimum pressure of the inlet streams".
        # The old fixed value of 1.0 was a trap: even when the feed was pressurized, the
        # membrane-inlet Mixer reset it to 1 bar and nullified the COMP.
        # Results are unchanged for existing all-1-bar configurations (regression
        # confirmed on the real tool, 2026-07-10).
        aspen.Tree.FindNode(rf"\Data\Blocks\{mixer_name}\Input\PRES").value  = 0.0
        for s in mixer_inputs[j]:
            aspen.Tree.FindNode(rf"\Data\Blocks\{mixer_name}\Ports\F(IN)").Elements.Add(s)
        aspen.Tree.FindNode(rf"\Data\Blocks\{mixer_name}\Ports\P(OUT)").Elements.Add(j)

    # --------------------------------------------------
    # Step 4: Create and connect unit blocks
    #   VP{n} (Compr, outlet=1 bar) is auto-inserted on the permeate side of each membrane
    # --------------------------------------------------
    auto_vps = []
    units = _group_arcs_by_unit(arcs)
    for unit_name, unit_arcs in units.items():
        _create_and_connect_unit(aspen, block_node, unit_name, unit_arcs,
                                 unit_params, mixer_set, auto_vps)

    # --------------------------------------------------
    # Step 5: Feed stream existence is established by Step 2 (feed vertices are sources
    # with outgoing arcs, so they are NOT terminal). The actual feed spec
    # (FLOWBASE/TOTFLOW/CARBO-01/NITRO-01) is applied later by AspenEvaluator via case.yaml.
    # The old _configure_feed (DAC default 420 ppm) is intentionally removed (ARCHITECTURE 10).
    # --------------------------------------------------

    return aspen, auto_vps


def set_continuous_variables(aspen, unit_params):
    """
    Write continuous variables (membrane area, permeate pressure, etc.) to Aspen.

    Parameters
    ----------
    unit_params : dict {unit_name: {param_name: value}}
    """
    for unit_name, params in unit_params.items():
        if "area" in params:
            node = aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Data\{unit_name}.A\VALUE"
            )
            if node is not None:
                node.value = params["area"]
        if "p_permeate" in params:
            node = aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Data\{unit_name}.PPERM\VALUE"
            )
            if node is not None:
                node.value = params["p_permeate"]
        # ver3 12.1 (Robeson membrane model): branch that writes permeance as a GA variable.
        # The node paths are identical to the initial setup in _create_membrane (this only
        # adds an interface).
        if "permeance_CO2" in params:
            node = aspen.Tree.FindNode(
                rf'\Data\Blocks\{unit_name}\Data\{unit_name}.L\{unit_name}.L("CARBO-01")\VALUE'
            )
            if node is not None:
                node.value = params["permeance_CO2"]
        if "permeance_N2" in params:
            node = aspen.Tree.FindNode(
                rf'\Data\Blocks\{unit_name}\Data\{unit_name}.L\{unit_name}.L("NITRO-01")\VALUE'
            )
            if node is not None:
                node.value = params["permeance_N2"]
        if "outlet_pressure" in params:
            node = aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Input\PRES"
            )
            if node is not None:
                node.value = params["outlet_pressure"]


# =========================================================
# Internal helpers
# =========================================================

def _num(vid):
    """Return the numeric part of a vertex ID (e.g. 'V7' -> '7')."""
    return vid[1:]


def _find_terminal_vertices(vertices, arcs, mixer_vertices):
    """
    Return terminal vertices for which no Aspen stream should be created.

    A vertex is terminal if ALL of the following hold:
      1. No outgoing arcs
      2. Not a Mixer vertex
      3. No unit block on any incoming arc (only no-op arcs)

    In ver2, sinks (product/residue) are always Mixer vertices (rule 3 in
    topology.mixer_vertices), so sinks are NEVER terminal — they get streams
    as measurement points.
    """
    has_outgoing = {frm for (frm, _) in arcs}
    terminal = set()
    for j in vertices:
        if j in mixer_vertices:
            continue
        # Condition 1: no outgoing arcs
        if j in has_outgoing:
            continue
        # Condition 3: no unit block on any incoming arc
        has_unit_incoming = any(
            arc_def.get("unit") for (i, jj), arc_def in arcs.items() if jj == j
        )
        if not has_unit_incoming:
            terminal.add(j)
    return terminal


def _group_arcs_by_unit(arc_definitions):
    """Group arc_definitions by unit name and return the result."""
    units = {}
    for (i, j), arc_def in arc_definitions.items():
        unit_name = arc_def.get("unit")
        if not unit_name:
            continue
        if unit_name not in units:
            units[unit_name] = []
        units[unit_name].append((i, j, arc_def))
    return units


def _out_stream(i, j, mixer_vertices):
    """Return the output stream name for arc (i→j)."""
    return f"VS{_num(i)}T{_num(j)}" if j in mixer_vertices else j


def _create_and_connect_unit(aspen, block_node, unit_name, arcs,
                              unit_params, mixer_vertices, auto_vps):
    """Create the Aspen block for a unit and connect its ports."""
    arc_types = {arc_def["type"] for _, _, arc_def in arcs}
    params    = unit_params.get(unit_name, {})

    if arc_types & {"membrane_permeate", "membrane_retentate"}:
        _create_membrane(aspen, block_node, unit_name, arcs, params, mixer_vertices, auto_vps)
    elif "compressor" in arc_types:
        _create_compressor(aspen, block_node, unit_name, arcs, params, mixer_vertices)
    elif "expander" in arc_types:
        _create_expander(aspen, block_node, unit_name, arcs, params, mixer_vertices)
    elif "heater" in arc_types:
        _create_heater(aspen, block_node, unit_name, arcs, params, mixer_vertices)
    else:
        # Silently skipping an unknown unit type would hand Aspen a broken flowsheet with
        # streams but no block, surfacing later as an unexplained convergence failure.
        # Fail explicitly instead, so the caller (_build) routes it to the BAD path and
        # its diagnostic print.
        raise ValueError(
            f"unit {unit_name!r}: unsupported arc types {sorted(arc_types)} "
            f"(no block builder registered)"
        )


# ver3 12.4 (user decision, 2026-07-10): cooling temperature [degC] applied automatically
# at the outlet of compression equipment (auto-VP and explicit COMP). Same as the membrane
# operating temperature in Lee (2018). Without intercooling, the heat of compression
# cascades downstream and inflates the power (Fig.3a reproduction: P_tot 1.38x the paper,
# vs 0.94x with 35 degC cooling), and it also exceeds the allowable temperature of polymeric
# membranes. Like the auto-VP, cooling is standard engineering equipment that the builder
# inserts automatically and that is not exposed to the GA/SST search. Heater duty is not
# electrical power, so it is not counted in energy (cooling-water cost is outside Lee's
# model as well).
AUTO_COOLER_TEMP_C = 35.0


def _attach_cooler(aspen, block_node, stream_node, source_port_path,
                   cooler_name, inter_stream, out):
    """Insert a 35 degC cooler (Heater, no pressure drop) at a compression block outlet.

    <source>.P(OUT) -> inter_stream -> {cooler_name}.F(IN), {cooler_name}.P(OUT) -> out
    """
    stream_node.Elements.Add(inter_stream)
    aspen.Tree.FindNode(source_port_path).Elements.Add(inter_stream)
    block_node.Elements.Add(f"{cooler_name}!Heater")
    aspen.Tree.FindNode(rf"\Data\Blocks\{cooler_name}\Input\TEMP").value = AUTO_COOLER_TEMP_C
    aspen.Tree.FindNode(rf"\Data\Blocks\{cooler_name}\Input\PRES").value = 0.0  # no pressure drop
    aspen.Tree.FindNode(rf"\Data\Blocks\{cooler_name}\Ports\F(IN)").Elements.Add(inter_stream)
    aspen.Tree.FindNode(rf"\Data\Blocks\{cooler_name}\Ports\P(OUT)").Elements.Add(out)


def _create_membrane(aspen, block_node, unit_name, arcs, params, mixer_vertices, auto_vps):
    """
    Create a GasPermModule block and connect its ports.

    Port assignment based on arc type:
      membrane_permeate  → Permeate(OUT)
      membrane_retentate → Retentate(OUT)
    Inlet (Inlet(IN)) = vid string (stream output by the Mixer at the src vertex)

    A VP{n} (Compr, outlet=1 bar) + auto-cooler is inserted on the permeate side:
      MEMB{n}.Permeate(OUT) -> VPI{n} -> VP{n} -> VPO{n} -> HXV{n}(35 degC) -> (original downstream)
    """
    stream_node = aspen.Tree.FindNode(r'\Data\Streams')
    memb_num = unit_name.replace("MEMB", "")

    block_node.Elements.Add(f"{unit_name}!GasPermModule")

    # Set parameters
    aspen.Tree.FindNode(
        rf'\Data\Blocks\{unit_name}\Data\{unit_name}.L\{unit_name}.L("CARBO-01")\VALUE'
    ).value = params.get("permeance_CO2", 2.70677)
    aspen.Tree.FindNode(
        rf'\Data\Blocks\{unit_name}\Data\{unit_name}.L\{unit_name}.L("NITRO-01")\VALUE'
    ).value = params.get("permeance_N2", 0.0541354)
    aspen.Tree.FindNode(
        rf"\Data\Blocks\{unit_name}\Data\{unit_name}.A\VALUE"
    ).value = params.get("area", 1000.0)
    aspen.Tree.FindNode(
        rf"\Data\Blocks\{unit_name}\Data\{unit_name}.PPERM\VALUE"
    ).value = params.get("p_permeate", 0.2)

    # Connect ports
    inlet_connected = False
    for i, j, arc_def in arcs:
        # Inlet: vid string (stream output by the Mixer)
        if not inlet_connected:
            aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Ports\Inlet(IN)"
            ).Elements.Add(i)
            inlet_connected = True

        # Outlet: port determined by arc type
        out = _out_stream(i, j, mixer_vertices)
        if arc_def["type"] == "membrane_permeate":
            # --- Auto-insert VP ---
            # MEMB{n}.Permeate(OUT) → VPI{n} → VP{n}.F(IN) → VP{n}.P(OUT) = out
            vp_name   = f"VP{memb_num}"
            vpi_name  = f"VPI{memb_num}"
            stream_node.Elements.Add(vpi_name)

            aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Ports\Permeate(OUT)"
            ).Elements.Add(vpi_name)

            block_node.Elements.Add(f"{vp_name}!Compr")
            aspen.Tree.FindNode(rf"\Data\Blocks\{vp_name}\Input\MODEL_TYPE").value = "COMPRESSOR"
            aspen.Tree.FindNode(rf"\Data\Blocks\{vp_name}\Input\TYPE").value       = "ISENTROPIC"
            aspen.Tree.FindNode(rf"\Data\Blocks\{vp_name}\Input\OPT_SPEC").value   = "PRES"
            aspen.Tree.FindNode(rf"\Data\Blocks\{vp_name}\Input\PRES").value       = 1.0
            aspen.Tree.FindNode(rf"\Data\Blocks\{vp_name}\Ports\F(IN)").Elements.Add(vpi_name)
            # Auto-cooling (12.4): VP{n} -> VPO{n} -> HXV{n}(35 degC) -> out
            _attach_cooler(
                aspen, block_node, stream_node,
                rf"\Data\Blocks\{vp_name}\Ports\P(OUT)",
                cooler_name=f"HXV{memb_num}", inter_stream=f"VPO{memb_num}", out=out,
            )

            auto_vps.append(vp_name)
        elif arc_def["type"] == "membrane_retentate":
            aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Ports\Retentate(OUT)"
            ).Elements.Add(out)


def _create_compressor(aspen, block_node, unit_name, arcs, params, mixer_vertices):
    """Create a Compr block and connect its ports (with an auto-cooler at the outlet, 12.4)."""
    stream_node = aspen.Tree.FindNode(r'\Data\Streams')
    comp_num = unit_name.replace("COMP", "")
    block_node.Elements.Add(f"{unit_name}!Compr")
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\MODEL_TYPE").value = "COMPRESSOR"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\TYPE").value       = "ISENTROPIC"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\OPT_SPEC").value   = "PRES"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\PRES").value = params.get("outlet_pressure", 1.0)

    for i, j, _ in arcs:
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\F(IN)").Elements.Add(i)
        # Auto-cooling (12.4): COMP{n} -> HCI{n} -> HXC{n}(35 degC) -> out
        _attach_cooler(
            aspen, block_node, stream_node,
            rf"\Data\Blocks\{unit_name}\Ports\P(OUT)",
            cooler_name=f"HXC{comp_num}", inter_stream=f"HCI{comp_num}",
            out=_out_stream(i, j, mixer_vertices),
        )


def _create_expander(aspen, block_node, unit_name, arcs, params, mixer_vertices):
    """Create a Compr block in TURBINE mode (expander, power recovery; ver3 12.4).

    Identical in form to _create_compressor (only MODEL_TYPE differs, TURBINE). The outlet
    pressure comes from params outlet_pressure (default 1.0 bar = discharge to atmosphere)
    and is not a GA variable. WNET appears in energy_breakdown as a negative value
    (recovered power) and is counted in the specific energy and cost.
    """
    block_node.Elements.Add(f"{unit_name}!Compr")
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\MODEL_TYPE").value = "TURBINE"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\TYPE").value       = "ISENTROPIC"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\OPT_SPEC").value   = "PRES"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\PRES").value = params.get("outlet_pressure", 1.0)

    for i, j, _ in arcs:
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\F(IN)").Elements.Add(i)
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\P(OUT)").Elements.Add(
            _out_stream(i, j, mixer_vertices)
        )


def _create_heater(aspen, block_node, unit_name, arcs, params, mixer_vertices):
    """Create a Heater block and connect its ports."""
    block_node.Elements.Add(f"{unit_name}!Heater")
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\TEMP").value = params.get("temperature", 25)
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\PRES").value = params.get("pressure", 1.0)

    for i, j, _ in arcs:
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\F(IN)").Elements.Add(i)
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\P(OUT)").Elements.Add(
            _out_stream(i, j, mixer_vertices)
        )