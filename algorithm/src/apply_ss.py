"""ss_change.json（ユニット単位の差分）を読み、ss_current.json を更新する。

操作（ARCHITECTURE 6.2）:
    add_unit          : ユニット追加（固定。出力頂点を STRUCTURE_TEMPLATES から決定論的に採番）
    add_gated_unit    : ユニットを GA トグルとして追加（給餌をバイパス候補化＝engagement を
                        バイナリ変数に。OFF 時は active_topology の pruning が刈り取る。6.2）
    delete_unit       : ユニット削除（流入は residue へ振替、流出は削除。6.3）
    add_arc           : アーク追加（candidate:true で q_k を自動採番）
    delete_arc        : アーク1本削除（unit 所有アークは拒否、delete_unit を使う。6.2）
    promote_candidate : 候補→固定アーク昇格（candidate キーを除去）
    set_bounds        : 連続変数の境界を units[name].bounds_override に保存

CLI:
    uv run python algorithm/src/apply_ss.py --base-dir runs/runN --iter N
    → <base>/iterations/iter_NNN/ss_change.json を <base>/ss_current.json に適用

退避と検証（ARCHITECTURE 6.2.1 / 7節）:
    - 適用前に必ず ss_current.json を iter_NNN/ss_before_change.json に退避する
    - 適用後 topology.validate を呼び、違反時は例外で停止し ss_current.json を更新しない
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import topology as T  # noqa: E402
from topology import allocate_candidate_id, allocate_vertex_id  # noqa: E402
from unit_registry import STRUCTURE_TEMPLATES, get_outlet_ports  # noqa: E402


# ユニット種別ごとの「内部アーク」型（inlet→outlet 頂点間のアーク）。
# 既定は "process"。MEMB と COMP は語彙3.6に従う。
INNER_ARC_TYPE: dict[str, dict[str, str]] = {
    "MEMB": {"permeate": "membrane_permeate", "retentate": "membrane_retentate"},
    "COMP": {"outlet": "compressor"},
    "EXP":  {"outlet": "expander"},   # 膨張機（ver3 12.4）
    "HEAT": {"outlet": "heater"},     # 冷却器/加熱器（ver3 12.4 検算で追加）
}


class ApplyError(ValueError):
    """ss_change の適用中に検出した不整合。"""


# =========================================================
# 操作ハンドラ
# =========================================================

def op_add_unit(ss: dict[str, Any], op: dict[str, Any]) -> None:
    unit_type = op["unit_type"]
    unit = op["unit"]
    inlet = op["inlet"]
    params = op.get("params", {})

    if unit_type not in STRUCTURE_TEMPLATES:
        raise ApplyError(f"unknown unit_type {unit_type!r}")
    if unit in ss["units"]:
        raise ApplyError(f"unit {unit!r} already exists")
    if inlet not in ss["vertices"]:
        raise ApplyError(f"inlet {inlet!r} not in vertices")

    ports = get_outlet_ports(unit_type)
    inner_map = INNER_ARC_TYPE.get(unit_type, {})

    # 各 outlet ポートの接続先（{port}_to）を取得
    port_to: dict[str, str] = {}
    for port in ports:
        key = f"{port}_to"
        if key not in op:
            raise ApplyError(f"add_unit for {unit_type} requires field {key!r}")
        target = op[key]
        if target not in ss["vertices"]:
            raise ApplyError(f"{key}={target!r} not in vertices")
        port_to[port] = target

    # 出力頂点を採番（STRUCTURE_TEMPLATES のポート順で決定論的）
    outlets: dict[str, str] = {}
    for port in ports:
        new_v = allocate_vertex_id(ss)
        ss["vertices"][new_v] = {
            "role": "internal",
            "label": f"{unit} {port} outlet",
        }
        outlets[port] = new_v

    # アーク追加（内部アーク＋後段アーク）。既存直結 (inlet, port_to) があれば削除。
    # ただし unit 所有アーク（他装置の内部構造）や candidate（バイナリ変数）を
    # 黙って壊すことは許さない（明示的な delete_unit / delete_arc を要求する）。
    for port in ports:
        outlet_v = outlets[port]
        downstream = port_to[port]
        inner_type = inner_map.get(port, "process")

        direct = (inlet, downstream)
        existing_direct = ss["arcs"].get(direct)
        if existing_direct is not None:
            owner = existing_direct.get("unit")
            if owner is not None:
                raise ApplyError(
                    f"direct arc {direct} is owned by unit {owner!r}; "
                    f"add_unit cannot silently replace it — "
                    f"use delete_unit first or choose a different inlet/{port}_to"
                )
            cand = existing_direct.get("candidate")
            if cand:
                raise ApplyError(
                    f"direct arc {direct} is candidate {cand!r}; "
                    f"delete_arc it explicitly first "
                    f"(a binary variable must not silently vanish)"
                )
            del ss["arcs"][direct]

        if (inlet, outlet_v) in ss["arcs"]:
            raise ApplyError(f"arc {(inlet, outlet_v)} already exists")
        ss["arcs"][(inlet, outlet_v)] = {"type": inner_type, "unit": unit}

        if (outlet_v, downstream) in ss["arcs"]:
            raise ApplyError(f"arc {(outlet_v, downstream)} already exists")
        ss["arcs"][(outlet_v, downstream)] = {"type": "process"}

    ss["units"][unit] = {
        "type": unit_type,
        "inlet": inlet,
        "outlets": outlets,
        "params": copy.deepcopy(params),
    }


def op_add_gated_unit(ss: dict[str, Any], op: dict[str, Any]) -> None:
    """ユニットを「GA が on/off を決めるトグル」として追加する（構造を GA 変数に）。

    `add_unit` が固定追加（ユニットが常に engaged）なのに対し、本オペは engagement を
    バイナリ候補（q_k）に帰属させる。具体的には専用の新インレット頂点を作り、その給餌を
    候補アークにする。給餌候補 OFF → インレット入次数0 → `active_topology` の
    dead-unit pruning がユニットを刈り取る＝「ユニットなしのクリーン下位構造」。

    展開（feed_from の流れを「ユニットへ ⇄ バイパス」のトグルペアに変える）:
        - 新インレット頂点 Vin（role=internal）を採番
        - 出力頂点を STRUCTURE_TEMPLATES のポート順で採番
        - 内部アーク Vin→outlet（固定・unit 所有）＋後段アーク outlet→{port}_to
        - 給餌候補 (feed_from→Vin)（candidate q_a）
        - バイパス候補 (feed_from→bypass_to)（candidate q_b）。既存の固定アークがあれば
          型を引き継いで候補化（相互排他にする）。unit 所有アークなら拒否。

    必須フィールド: unit_type / unit / feed_from / bypass_to / 各 {port}_to。
    注意: feed_from は「インターセプトする1本の流れ」の源であること。feed_from に他の固定
    出アークが残っていると候補 ON 時に出次数>1 で is_buildable が弾く（リサイクルのトグル
    ペアと同じ規律。playbook 参照）。バイナリを2本消費する（max_binary_variables を自己点検）。
    """
    unit_type = op["unit_type"]
    unit = op["unit"]
    feed_from = op["feed_from"]
    bypass_to = op["bypass_to"]
    params = op.get("params", {})

    if unit_type not in STRUCTURE_TEMPLATES:
        raise ApplyError(f"unknown unit_type {unit_type!r}")
    if unit in ss["units"]:
        raise ApplyError(f"unit {unit!r} already exists")
    if feed_from not in ss["vertices"]:
        raise ApplyError(f"feed_from {feed_from!r} not in vertices")
    if bypass_to not in ss["vertices"]:
        raise ApplyError(f"bypass_to {bypass_to!r} not in vertices")

    ports = get_outlet_ports(unit_type)
    inner_map = INNER_ARC_TYPE.get(unit_type, {})

    # 各 outlet ポートの接続先（{port}_to）を取得
    port_to: dict[str, str] = {}
    for port in ports:
        key = f"{port}_to"
        if key not in op:
            raise ApplyError(f"add_gated_unit for {unit_type} requires field {key!r}")
        target = op[key]
        if target not in ss["vertices"]:
            raise ApplyError(f"{key}={target!r} not in vertices")
        port_to[port] = target

    # バイパス候補の対象（feed_from→bypass_to）の事前チェック
    bypass_key = (feed_from, bypass_to)
    existing_bypass = ss["arcs"].get(bypass_key)
    if existing_bypass is not None and existing_bypass.get("unit") is not None:
        raise ApplyError(
            f"bypass arc {bypass_key} is owned by unit {existing_bypass['unit']!r}; "
            f"choose a different feed_from/bypass_to"
        )
    if existing_bypass is not None and existing_bypass.get("candidate"):
        # 既に候補のアークに重ねると既存 q ラベルが黙って消え、履歴追跡・
        # promote_candidate の参照が壊れる。仕様外（6.2 は「固定アークの候補化」のみ）。
        raise ApplyError(
            f"bypass arc {bypass_key} is already candidate "
            f"{existing_bypass['candidate']!r}; resolve the existing toggle first "
            f"(promote_candidate or delete_arc) before gating this stream"
        )

    # 1. 新インレット頂点
    inlet = allocate_vertex_id(ss)
    ss["vertices"][inlet] = {"role": "internal", "label": f"{unit} inlet (gated)"}

    # 2. 出力頂点を採番
    outlets: dict[str, str] = {}
    for port in ports:
        new_v = allocate_vertex_id(ss)
        ss["vertices"][new_v] = {"role": "internal", "label": f"{unit} {port} outlet"}
        outlets[port] = new_v

    # 3. 内部アーク（固定・unit 所有）＋後段アーク
    for port in ports:
        outlet_v = outlets[port]
        downstream = port_to[port]
        inner_type = inner_map.get(port, "process")
        ss["arcs"][(inlet, outlet_v)] = {"type": inner_type, "unit": unit}
        if (outlet_v, downstream) in ss["arcs"]:
            raise ApplyError(f"arc {(outlet_v, downstream)} already exists")
        ss["arcs"][(outlet_v, downstream)] = {"type": "process"}

    # 4. トグルペア：給餌候補 q_a → バイパス候補 q_b の順で採番（distinct）
    feed_key = (feed_from, inlet)
    if feed_key in ss["arcs"]:
        raise ApplyError(f"arc {feed_key} already exists")
    ss["arcs"][feed_key] = {"type": "process", "candidate": allocate_candidate_id(ss)}

    bypass_type = existing_bypass["type"] if existing_bypass else "process"
    ss["arcs"][bypass_key] = {
        "type": bypass_type,
        "candidate": allocate_candidate_id(ss),
    }

    # 5. ユニット登録
    ss["units"][unit] = {
        "type": unit_type,
        "inlet": inlet,
        "outlets": outlets,
        "params": copy.deepcopy(params),
    }


def op_delete_unit(ss: dict[str, Any], op: dict[str, Any]) -> None:
    unit = op["unit"]
    reroute_to_opt = op.get("reroute_to")

    if unit not in ss["units"]:
        raise ApplyError(f"unit {unit!r} not found")
    u = ss["units"][unit]
    inlet_v = u["inlet"]
    outlet_vs = list(u["outlets"].values())

    # 1. 流出側: outlet 頂点とそれに触れる全アークを削除
    for ov in outlet_vs:
        keys = [k for k in ss["arcs"] if ov in k]
        for k in keys:
            del ss["arcs"][k]
        if ov in ss["vertices"]:
            del ss["vertices"][ov]

    # units 辞書から削除（以降の参照判定で除外するため先に消す）
    del ss["units"][unit]

    # 2. inlet_v が他ユニットから参照されているか
    inlet_in_other_units = any(
        inlet_v == ud.get("inlet") or inlet_v in (ud.get("outlets") or {}).values()
        for ud in ss["units"].values()
    )
    if inlet_in_other_units:
        # 生きた経路を壊さない（Q3）
        return

    # 3. inlet_v にまだ outgoing アークが残っていれば触らない（生きた経路）
    outgoing = [k for k in ss["arcs"] if k[0] == inlet_v]
    if outgoing:
        return

    # 4. 流入を residue へ振替
    incoming = [k for k in ss["arcs"] if k[1] == inlet_v]
    if incoming:
        sink = _resolve_reroute_sink(ss, reroute_to_opt)
        for (frm, to) in incoming:
            meta = ss["arcs"][(frm, to)]
            del ss["arcs"][(frm, to)]
            new_meta = {k: v for k, v in meta.items() if k != "unit"}
            new_meta["type"] = "residue"
            if (frm, sink) in ss["arcs"]:
                raise ApplyError(
                    f"cannot reroute {(frm, to)} → {(frm, sink)}: target arc already exists"
                )
            ss["arcs"][(frm, sink)] = new_meta

    # 5. inlet_v が孤立していれば削除（Q2）
    has_remaining = any(inlet_v in k for k in ss["arcs"])
    if not has_remaining:
        del ss["vertices"][inlet_v]


def _resolve_reroute_sink(ss: dict[str, Any], reroute_to_opt: str | None) -> str:
    """delete_unit の流入振替先を決定。reroute_to があればそれ、なければ role=residue を自動探索。"""
    if reroute_to_opt is not None:
        if reroute_to_opt not in ss["vertices"]:
            raise ApplyError(f"reroute_to={reroute_to_opt!r} not in vertices")
        return reroute_to_opt
    sinks = [v for v, d in ss["vertices"].items() if d.get("role") == "residue"]
    if not sinks:
        raise ApplyError("no residue sink found and reroute_to not specified")
    return sinks[0]


def op_add_arc(ss: dict[str, Any], op: dict[str, Any]) -> None:
    frm = op["from"]
    to = op["to"]
    arc_type = op["type"]
    candidate = bool(op.get("candidate", False))

    # unit フィールドは受け付けない：所有アーク（装置の内部構造）は add_unit /
    # add_gated_unit だけが張る。誤タグの所有アークは validate の所有権検査でも
    # 弾かれるが、入口で明示的に拒否した方が提案の修正が速い。
    if op.get("unit") is not None:
        raise ApplyError(
            f"add_arc must not carry a unit field (got unit={op['unit']!r}); "
            f"owned arcs are created only by add_unit / add_gated_unit"
        )

    if frm not in ss["vertices"]:
        raise ApplyError(f"from={frm!r} not in vertices")
    if to not in ss["vertices"]:
        raise ApplyError(f"to={to!r} not in vertices")
    if frm == to:
        raise ApplyError(f"self-loop arc {(frm, to)} is not allowed")
    if (frm, to) in ss["arcs"]:
        raise ApplyError(f"arc {(frm, to)} already exists")

    meta: dict[str, Any] = {"type": arc_type}
    if candidate:
        meta["candidate"] = allocate_candidate_id(ss)
    ss["arcs"][(frm, to)] = meta


def op_delete_arc(ss: dict[str, Any], op: dict[str, Any]) -> None:
    """(from, to) のアークを1本削除する（op_add_arc の鏡像）。

    ガードは meta の unit 有無だけ：ユニット所有アーク（膜の permeate/retentate,
    COMP の内部アーク）は単体削除すると装置が壊れるので拒否し、delete_unit を使わせる。
    削除で頂点が孤立・行き止まりになるケースは apply 後の validate（検査項目8）が弾く。
    """
    frm = op["from"]
    to = op["to"]
    key = (frm, to)
    if key not in ss["arcs"]:
        raise ApplyError(f"arc {key} not found")
    owner = ss["arcs"][key].get("unit")
    if owner is not None:
        raise ApplyError(
            f"arc {key} is owned by unit {owner!r}; use delete_unit instead"
        )
    # ゾンビユニット化ガード：to がいずれかの unit の inlet で、削除するとその inlet への
    # 給餌アークが SS 上から 1 本も無くなる場合は拒否する。給餌経路を失った unit は
    # active_topology の pruning がどの q でも刈るため「絶対に建たないのに連続変数だけ
    # GA 染色体に残り続ける」状態になり、validate（項目7/8 の対象外）でも検出できない。
    # ユニットごと消したいなら delete_unit を使う（所有アーク拒否と同じ役割分担）。
    for uname, udef in ss["units"].items():
        if udef.get("inlet") != to:
            continue
        remaining_in = [k for k in ss["arcs"] if k[1] == to and k != key]
        if not remaining_in:
            raise ApplyError(
                f"deleting arc {key} would leave unit {uname!r} with no feed arc "
                f"(it could never be built again = zombie unit); "
                f"use delete_unit {uname!r} instead"
            )
    del ss["arcs"][key]


def op_promote_candidate(ss: dict[str, Any], op: dict[str, Any]) -> None:
    cand = op["candidate"]
    found = 0
    for meta in ss["arcs"].values():
        if meta.get("candidate") == cand:
            del meta["candidate"]
            found += 1
    if found == 0:
        raise ApplyError(f"candidate {cand!r} not found in arcs")


def op_set_bounds(ss: dict[str, Any], op: dict[str, Any]) -> None:
    unit = op["unit"]
    param = op["param"]
    bounds = op["bounds"]

    if unit not in ss["units"]:
        raise ApplyError(f"unit {unit!r} not found")
    if not (isinstance(bounds, list) and len(bounds) == 2):
        raise ApplyError(f"bounds must be a [lo, hi] list: {bounds!r}")
    ss["units"][unit].setdefault("bounds_override", {})[param] = list(bounds)


OP_HANDLERS = {
    "add_unit":          op_add_unit,
    "add_gated_unit":    op_add_gated_unit,
    "delete_unit":       op_delete_unit,
    "add_arc":           op_add_arc,
    "delete_arc":        op_delete_arc,
    "promote_candidate": op_promote_candidate,
    "set_bounds":        op_set_bounds,
}


# =========================================================
# 適用本体
# =========================================================

def apply_change(ss: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """ss に change を適用して新しい SS を返す（in-place ではない）。

    history への append と iteration の +1 もここで行う。
    validate は呼ばない（main / apply_from_files 側で行う）。
    """
    # スキーマガード（run24 iter_001 の実事故対策）: エージェントが "changes" 等の
    # 誤ったトップレベルキーで書くと、旧実装は「0操作の空適用」を黙って成功させ、
    # 気づかないまま同じ SS で次の反復（数時間）を走らせてしまう。operations が
    # 無い・空・リストでない場合は明示的に失敗させる。
    ops = change.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ApplyError(
            "ss_change に operations（非空リスト）がありません。"
            f"トップレベルキー: {sorted(change.keys())}。"
            "正しいスキーマは {\"reason\": ..., \"operations\": [...]}"
            "（ARCHITECTURE 6.2）"
        )

    new_ss = copy.deepcopy(ss)
    new_ss["iteration"] = ss.get("iteration", 0) + 1

    for i, op in enumerate(ops):
        name = op.get("op")
        handler = OP_HANDLERS.get(name)
        if handler is None:
            raise ApplyError(f"operations[{i}]: unknown op {name!r}")
        try:
            handler(new_ss, op)
        except ApplyError as e:
            raise ApplyError(f"operations[{i}] ({name}): {e}") from e

    new_ss.setdefault("history", []).append({
        "iter": new_ss["iteration"],
        "change": change.get("reason", ""),
    })
    return new_ss


def apply_from_files(base_dir: str, iter_num: int, force: bool = False) -> dict[str, Any]:
    """ss_change.json を読んで ss_current.json を更新する（CLI 本体）。

    - 適用前に必ず ss_current.json を ss_before_change.json に退避（ARCH 7節）。
    - 退避先が既に存在する場合は「適用済みの再実行」とみなして拒否する（force=True で
      のみ上書き許可）。無条件上書きだとリトライ1回でロールバック起点（変更前 SS）が
      適用後の状態に化けて永久に失われる。
    - 適用 → topology.validate → save の順。validate 失敗時は例外で停止し
      ss_current.json は更新されない（ARCH 6.2.1）。
    """
    iter_dir = os.path.join(base_dir, f"iterations/iter_{iter_num:03d}")
    change_file = os.path.join(iter_dir, "ss_change.json")
    ss_path = os.path.join(base_dir, "ss_current.json")
    backup_path = os.path.join(iter_dir, "ss_before_change.json")

    if not os.path.exists(change_file):
        raise FileNotFoundError(f"ss_change.json not found: {change_file}")
    if not os.path.exists(ss_path):
        raise FileNotFoundError(f"ss_current.json not found: {ss_path}")
    if os.path.exists(backup_path) and not force:
        raise ApplyError(
            f"{backup_path} already exists — iter {iter_num} was likely applied already. "
            f"If you really intend to re-apply (e.g. after restoring from the backup), "
            f"run again with --force (this overwrites the rollback backup)."
        )

    with open(change_file, "r", encoding="utf-8") as f:
        change = json.load(f)
    ss = T.load_ss(ss_path)

    shutil.copy(ss_path, backup_path)

    new_ss = apply_change(ss, change)
    T.validate(new_ss)
    T.save_ss(new_ss, ss_path)
    return new_ss


# =========================================================
# CLI
# =========================================================

def get_latest_iter_num(base_dir: str) -> int | None:
    """iterations/ 配下で ss_change.json が存在する最大の iter 番号を返す。"""
    iter_base = os.path.join(base_dir, "iterations")
    if not os.path.exists(iter_base):
        return None
    nums: list[int] = []
    for d in os.listdir(iter_base):
        if not d.startswith("iter_"):
            continue
        change_path = os.path.join(iter_base, d, "ss_change.json")
        if not os.path.exists(change_path):
            continue
        try:
            nums.append(int(d.split("_", 1)[1]))
        except ValueError:
            continue
    return max(nums) if nums else None


def _load_membrane_model() -> dict[str, Any] | None:
    """case.yaml の membrane_model を表示用に読む（無ければ None・失敗しても落とさない）。

    apply_ss はロジック上 case に依存しないが、サマリの連続変数の本数・名前を
    GA/BO（membrane_model 込みで導出）と一致させるためだけに参照する。
    """
    case_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "case.yaml")
    )
    try:
        import yaml
        with open(case_path, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("membrane_model")
    except Exception:
        return None


def print_ss_summary(ss: dict[str, Any]) -> None:
    bvars = T.binary_variables(ss)
    cvars = T.continuous_variables(ss, _load_membrane_model())
    print()
    print("Updated SS:")
    print(f"  Iteration: {ss['iteration']}")
    print(f"  Vertices:  {len(ss['vertices'])}  {list(ss['vertices'])}")
    print(f"  Arcs:      {len(ss['arcs'])}")
    print(f"  Units:     {list(ss['units'])}")
    print(f"  Binary variables     ({len(bvars)}): {[b['name'] for b in bvars]}")
    print(f"  Continuous variables ({len(cvars)}): {[c['name'] for c in cvars]}")


def main() -> None:
    # Windows の既定コンソールは cp932 で、ss_change の reason に含まれる非 cp932 文字
    # （矢印・記号等）を print すると UnicodeEncodeError で落ちる。自律ループは reason を
    # 任意の Unicode で生成するため、標準ストリームを UTF-8 に固定して堅牢化する。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".",
                        help="run directory containing ss_current.json and iterations/")
    parser.add_argument("--iter", type=int, default=None,
                        help="iteration number to apply (auto-detected if omitted)")
    parser.add_argument("--force", action="store_true",
                        help="re-apply even if ss_before_change.json already exists "
                             "(overwrites the rollback backup — use only intentionally)")
    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    iter_num = args.iter if args.iter is not None else get_latest_iter_num(base_dir)
    if iter_num is None:
        print("Error: no iteration with ss_change.json found", file=sys.stderr)
        sys.exit(1)

    change_path = os.path.join(base_dir, f"iterations/iter_{iter_num:03d}/ss_change.json")
    if not os.path.exists(change_path):
        print(f"Error: {change_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(change_path, "r", encoding="utf-8") as f:
        change_preview = json.load(f)

    print("=" * 60)
    print(f"Applying SS change: iter {iter_num} → {iter_num + 1}")
    print(f"Reason: {change_preview.get('reason', '(not stated)')}")
    print("=" * 60)

    new_ss = apply_from_files(base_dir, iter_num, force=args.force)
    print_ss_summary(new_ss)
    print(f"\n{os.path.join(base_dir, 'ss_current.json')} updated")


if __name__ == "__main__":
    main()
