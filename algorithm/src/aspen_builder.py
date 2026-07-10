"""Dynamically build an Aspen model from a concrete topology (具体トポロジー).

旧 LLM4ChemPro/algorithm/src/aspen_builder.py からの忠実移植。
変更は ARCHITECTURE 3.2 / 3.4 / 4節 / 10節 に定めた3点に限定する:

  (1) 入力インターフェース
      旧: (adj_matrix, arc_definitions, unit_params, aspen_file)
      新: (topology, aspen_file)。topology は {vertices, arcs, units} の3辞書（具体トポロジー）。
      整数インデックスではなく安定文字列ID（V7 等）をそのまま Aspen ストリーム名に使う。
      補助名 VS{i}T{j} / MIXV{j} / DV{i} / VPI{n} / VP{n} の {i}/{j}/{n} は vid・unit名の数値部分。

  (2) Mixer 規則に (3) sink 頂点を追加
      旧 _find_mixer_vertices は規則 (1)(2) のみ。ver2 では sink (role=product/residue) を
      Mixer 化してストリーム化することで純度/回収率の測定点を sink 上に固定する（ARCHITECTURE 3.4）。
      規則は topology.mixer_vertices に一元化済みなので、そこに委譲する。
      これに伴い _find_mixer_vertices は削除（重複定義を避ける）。

  (3) feed 既定の廃止
      旧 _configure_feed は DAC 既定（420ppm, MOLE-FRAC 等）を持っていたが削除する。
      builder 内に feed 既定は持たず、feed ストリームの存在だけ Step 2 で確保する。
      組成・流量・基準（FLOWBASE/TOTFLOW/CARBO-01/NITRO-01）の上書きは
      AspenEvaluator 側（フェーズ4）で case.yaml から行う（ARCHITECTURE 10節）。

上記以外（膜ブロック生成、VP自動挿入、命名規則、ポート接続のロジック）は旧実装どおり。

Stream naming:
  V{i}      : representative stream for vertex (= vertex ID such as "V7")
  VS{i}T{j} : intermediate stream entering a Mixer vertex ({i}/{j} are vid numeric parts)
  DV{i}     : discard stream for FSplit ({i} is vid numeric part)
  MIXV{j}   : Mixer block name ({j} is vid numeric part)
  MEMB{n}   : membrane block (unit name as-is)
  VP{n}/VPI{n} : auto-VP block and its inlet stream (n matches MEMB{n})

NOTE: Aspen does not allow underscores in block or stream names.
"""

import os
import subprocess
import sys
import win32com.client as win32

sys.path.insert(0, os.path.dirname(__file__))
from topology import mixer_vertices as _topology_mixer_vertices  # noqa: E402


def kill_aspen_image(timeout: float = 20) -> None:
    """taskkill /f /im AspenPlus.exe をタイムアウト付きで実行する（子プロセス側の後始末用）。

    os.system はタイムアウトが無く、kill 不能な wedge Aspen で呼び出し元ごと無制限に
    ブロックし得る（run13 の 85 分ハングの主因と同型。subprocess_evaluator 側は修正済み）。
    子プロセス（worker）内の Aspen 後始末はすべて本関数を経由する。
    失敗・タイムアウトは握りつぶす——後始末の失敗で評価ラインを止めないことが最優先で、
    残留 Aspen は次のビルド起動時の taskkill か親の wedge 処理が回収する。
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
    #   (3) sink (product/residue) vertices  ← ver2 で追加（測定点をストリーム化）
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
            # Unit arc (membrane included). 出口名は VS{_num(i)}T{_num(j)}。
            # ポート接続は unit 側（_create_membrane / _create_compressor 等）が行うが、
            # ストリーム生成(Step2)とミキサー F(IN)への登録(Step3)はここで積む必要がある。
            # 膜だけ pass で抜けていたのが3段ビルド失敗の原因（VS{i}T{j} 未生成）。
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
        # ver3 12.4: PRES=0 ＝「入口ストリームの最小圧に追従」。旧値 1.0（固定）は
        # feed を昇圧しても膜入口 Mixer で 1 bar に戻し COMP を無効化する罠だった。
        # 全ストリーム 1 bar の既存構成では結果不変（2026-07-10 実機回帰確認済み）。
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
        # ver3 12.1（Robeson 膜モデル）: permeance を GA 変数として書き込む分岐。
        # ノードパスは _create_membrane の初期設定と同一（インターフェース追加のみ）。
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
        # 未知のユニット型を黙ってスキップすると「ブロックが無いのにストリームだけある」
        # 壊れたフローシートが無言で Aspen に渡り、原因不明の非収束として現れる。
        # 明示的に失敗させ、呼び出し側（_build）の BAD 経路＋診断 print に乗せる。
        raise ValueError(
            f"unit {unit_name!r}: unsupported arc types {sorted(arc_types)} "
            f"(no block builder registered)"
        )


def _create_membrane(aspen, block_node, unit_name, arcs, params, mixer_vertices, auto_vps):
    """
    Create a GasPermModule block and connect its ports.

    Port assignment based on arc type:
      membrane_permeate  → Permeate(OUT)
      membrane_retentate → Retentate(OUT)
    Inlet (Inlet(IN)) = vid string (stream output by the Mixer at the src vertex)

    A VP{n} (Compr, outlet=1 bar) is auto-inserted on the permeate side:
      MEMB{n}.Permeate(OUT) → VPI{n} → VP{n}.F(IN) → VP{n}.P(OUT) → (original downstream stream)
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
            aspen.Tree.FindNode(rf"\Data\Blocks\{vp_name}\Ports\P(OUT)").Elements.Add(out)

            auto_vps.append(vp_name)
        elif arc_def["type"] == "membrane_retentate":
            aspen.Tree.FindNode(
                rf"\Data\Blocks\{unit_name}\Ports\Retentate(OUT)"
            ).Elements.Add(out)


def _create_compressor(aspen, block_node, unit_name, arcs, params, mixer_vertices):
    """Create a Compr block and connect its ports."""
    block_node.Elements.Add(f"{unit_name}!Compr")
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\MODEL_TYPE").value = "COMPRESSOR"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\TYPE").value       = "ISENTROPIC"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\OPT_SPEC").value   = "PRES"
    aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Input\PRES").value = params.get("outlet_pressure", 1.0)

    for i, j, _ in arcs:
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\F(IN)").Elements.Add(i)
        aspen.Tree.FindNode(rf"\Data\Blocks\{unit_name}\Ports\P(OUT)").Elements.Add(
            _out_stream(i, j, mixer_vertices)
        )


def _create_expander(aspen, block_node, unit_name, arcs, params, mixer_vertices):
    """Create a Compr block in TURBINE mode（膨張機・電力回収。ver3 12.4）.

    _create_compressor と同型（MODEL_TYPE のみ TURBINE）。出口圧は params の
    outlet_pressure（既定 1.0 bar＝大気放出）で、GA 変数は持たない。WNET は負値
    （回収電力）で energy_breakdown に載り、比エネルギー・コストに算入される。
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