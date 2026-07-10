"""安定文字列IDのアーク辞書ベースのトポロジー表現。

責務:
- SS テンプレート（candidate を含む超構造定義）の読み書き
- 具体トポロジー（q 固定後）の生成: active_topology
- 妥当性検査: validate（違反時 TopologyError）
- 補助ユニットの導出: mixer_vertices / splitter_vertices / auto_vps
- 頂点ID・候補IDの採番（max+1、再付番しない）
- GA 変数リストの導出: binary_variables / continuous_variables
- 論文用の隣接行列生成: to_matrix

メモリ表現:
    ss = {
        "iteration": int,
        "vertices": {"V0": {"role": ..., "label": ...}, ...},
        "arcs":     {("V0", "V1"): {"type": ..., "unit": ..., "candidate": "q_k"?}, ...},
        "units":    {"MEMB1": {"type": ..., "inlet": ..., "outlets": {...}, "params": {...}}, ...},
        "history":  [...],
    }

ディスク表現（JSON）はタプルキーを持てないため arcs はリスト形式。
load_ss / save_ss が相互変換する。

依存: Python stdlib のみ（numpy は to_matrix の中でのみ import）。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Iterable
from os.path import dirname
from typing import Any

# 同ディレクトリの unit_registry を import するためパスを足す
sys.path.insert(0, dirname(__file__))
from unit_registry import (  # noqa: E402
    get_unit_type,
    is_tie_mode,
    make_ga_variables,
    permeance_bounds_aspen,
)


SinkRoles = frozenset({"product", "residue"})
SourceRoles = frozenset({"feed"})
MembraneArcTypes = frozenset({"membrane_permeate", "membrane_retentate"})


class TopologyError(ValueError):
    """SS / 具体トポロジーの構造的不整合を示す例外。"""


# =========================================================
# ID utilities
# =========================================================

def _vid_num(vid: str) -> int:
    """文字列ID "V12" → 12。"""
    if not vid.startswith("V"):
        raise TopologyError(f"vertex id must start with 'V': {vid!r}")
    try:
        return int(vid[1:])
    except ValueError as e:
        raise TopologyError(f"vertex id has no numeric suffix: {vid!r}") from e


def _qid_num(qid: str) -> int:
    """候補ID "q_3" → 3。"""
    if not qid.startswith("q_"):
        raise TopologyError(f"candidate id must start with 'q_': {qid!r}")
    try:
        return int(qid.split("_", 1)[1])
    except ValueError as e:
        raise TopologyError(f"candidate id has no numeric suffix: {qid!r}") from e


def next_vertex_id(vertices: dict[str, Any]) -> str:
    """現存頂点IDの数値部分 +1 を返す純関数（カウンタ初期化・フォールバック用）。

    vertices が空なら "V0"。
    注意: 「過去に払い出した最大」は知らないため、最大番号の頂点を削除した後に
    呼ぶと削除済みIDを再利用してしまう。SS への新規採番は allocate_vertex_id を使う
    （ARCHITECTURE 3.1「削除しても再付番・再利用しない」）。
    """
    if not vertices:
        return "V0"
    return f"V{max(_vid_num(v) for v in vertices) + 1}"


def next_candidate_id(arcs: dict[tuple[str, str], dict[str, Any]]) -> str:
    """現存候補IDの数値部分 +1 を返す純関数（カウンタ初期化・フォールバック用）。

    候補が一つもなければ "q_1"（1始まり）。
    注意: next_vertex_id と同じ理由で、SS への新規採番は allocate_candidate_id を使う。
    """
    nums: list[int] = []
    for meta in arcs.values():
        cand = meta.get("candidate")
        if cand:
            nums.append(_qid_num(cand))
    return f"q_{max(nums) + 1}" if nums else "q_1"


def _ensure_id_counters(ss: dict[str, Any]) -> dict[str, int]:
    """ss["id_counters"]（過去に払い出した最大番号）を保証して返す。

    キーが無い旧形式の SS（ss_seed.json 等）は現存最大値から初期化する（後方互換）。
    手編集等でカウンタが現存最大より遅れている場合も現存最大まで引き上げる（防御）。
    """
    counters = ss.get("id_counters") or {}
    v_max = max((_vid_num(v) for v in ss.get("vertices", {})), default=-1)
    q_max = 0
    for meta in ss.get("arcs", {}).values():
        cand = meta.get("candidate")
        if cand:
            q_max = max(q_max, _qid_num(cand))
    counters["vertex"] = max(int(counters.get("vertex", -1)), v_max)
    counters["candidate"] = max(int(counters.get("candidate", 0)), q_max)
    ss["id_counters"] = counters
    return counters


def allocate_vertex_id(ss: dict[str, Any]) -> str:
    """SS に新しい頂点IDを払い出す（削除済みIDを再利用しない・ARCHITECTURE 3.1）。

    「過去に払い出した最大＋1」。カウンタは ss["id_counters"] に永続化される
    （save_ss で JSON に残る）ので、最大番号の頂点を削除→再追加しても
    同じIDが別の物理的な流れに再割当されることはない。
    """
    counters = _ensure_id_counters(ss)
    counters["vertex"] += 1
    return f"V{counters['vertex']}"


def allocate_candidate_id(ss: dict[str, Any]) -> str:
    """SS に新しい候補ラベル q_k を払い出す（削除済みラベルを再利用しない）。"""
    counters = _ensure_id_counters(ss)
    counters["candidate"] += 1
    return f"q_{counters['candidate']}"


# =========================================================
# Load / Save
# =========================================================

def load_ss(path: str) -> dict[str, Any]:
    """JSON から SS を読み込み、メモリ表現（タプルキー arcs）に変換する。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return _from_json(raw)


def save_ss(ss: dict[str, Any], path: str) -> None:
    """SS を JSON に保存する。arcs は from/to の数値順で安定ソート。

    書き込みはアトミック（同一ディレクトリの一時ファイル → os.replace）。
    ss_current.json はライブ状態の正本であり、書き込み途中の強制終了
    （Aspen 巻き添えの taskkill / Ctrl-C / ディスクフル）で不正 JSON に
    なると外側ループ全体が止まるため、旧ファイルを無傷で残す。
    """
    raw = _to_json(ss)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)  # 同一ボリューム内なら Windows でも原子的


def _from_json(raw: dict[str, Any]) -> dict[str, Any]:
    arcs: dict[tuple[str, str], dict[str, Any]] = {}
    for arc in raw.get("arcs", []):
        frm = arc["from"]
        to = arc["to"]
        if (frm, to) in arcs:
            raise TopologyError(f"duplicate arc in json: {(frm, to)}")
        meta = {k: v for k, v in arc.items() if k not in ("from", "to")}
        arcs[(frm, to)] = meta
    ss = {
        "iteration": raw.get("iteration", 0),
        "vertices": copy.deepcopy(raw.get("vertices", {})),
        "arcs": arcs,
        "units": copy.deepcopy(raw.get("units", {})),
        "history": list(raw.get("history", [])),
    }
    if "id_counters" in raw:
        ss["id_counters"] = dict(raw["id_counters"])
    # 旧形式（id_counters なし）は現存最大から初期化（後方互換・ss_seed.json は変更不要）
    _ensure_id_counters(ss)
    return ss


def _to_json(ss: dict[str, Any]) -> dict[str, Any]:
    arc_keys = sorted(ss["arcs"], key=lambda k: (_vid_num(k[0]), _vid_num(k[1])))
    arcs_list = []
    for (frm, to) in arc_keys:
        meta = ss["arcs"][(frm, to)]
        arcs_list.append({"from": frm, "to": to, **meta})
    out = {
        "iteration": ss.get("iteration", 0),
        "vertices": copy.deepcopy(ss.get("vertices", {})),
        "arcs": arcs_list,
        "units": copy.deepcopy(ss.get("units", {})),
        "history": list(ss.get("history", [])),
    }
    if "id_counters" in ss:
        out["id_counters"] = dict(ss["id_counters"])
    return out


def dump_topology(topology: dict[str, Any]) -> dict[str, Any]:
    """具体トポロジー（タプルキー arcs）を JSON 化可能な dict に変換する。

    プロセス境界（subprocess_evaluator → aspen_worker）でトポロジーを渡すための
    直列化。SS と違い iteration/history は持たない（具体トポロジーは {vertices, arcs, units}）。
    arcs は from/to の数値順で安定ソート（_to_json と同じ規則）。
    """
    arc_keys = sorted(topology["arcs"], key=lambda k: (_vid_num(k[0]), _vid_num(k[1])))
    arcs_list = [
        {"from": frm, "to": to, **topology["arcs"][(frm, to)]} for (frm, to) in arc_keys
    ]
    return {
        "vertices": copy.deepcopy(topology["vertices"]),
        "arcs": arcs_list,
        "units": copy.deepcopy(topology.get("units", {})),
    }


def load_topology(raw: dict[str, Any]) -> dict[str, Any]:
    """dump_topology の逆。list 形式 arcs をタプルキー dict に戻す。"""
    arcs: dict[tuple[str, str], dict[str, Any]] = {}
    for arc in raw.get("arcs", []):
        frm = arc["from"]
        to = arc["to"]
        meta = {k: v for k, v in arc.items() if k not in ("from", "to")}
        arcs[(frm, to)] = meta
    return {
        "vertices": copy.deepcopy(raw.get("vertices", {})),
        "arcs": arcs,
        "units": copy.deepcopy(raw.get("units", {})),
    }


# =========================================================
# Active topology (q resolution)
# =========================================================

def active_topology(
    ss: dict[str, Any],
    q_active: dict[str, int] | None = None,
) -> dict[str, Any]:
    """SS テンプレート + q の値 → 具体トポロジー（3辞書）。

    Parameters
    ----------
    ss : SS テンプレート（candidate アークを含み得る）。
    q_active : 候補ラベル → 0/1 の辞書。未指定の候補は 0 とみなす。

    Returns
    -------
    {"vertices": {...}, "arcs": {...}, "units": {...}}
        具体トポロジー。arcs から `candidate` キーは外す（解決済み）。
        候補アーク OFF で給餌の消えた unit は `_prune_dead_units` が刈り取るので、
        返るトポロジーは「q のもとで実際に建つもの」と一致する。
    """
    q_active = q_active or {}
    vertices = copy.deepcopy(ss["vertices"])
    units = copy.deepcopy(ss.get("units", {}))

    arcs: dict[tuple[str, str], dict[str, Any]] = {}
    for key, meta in ss["arcs"].items():
        cand = meta.get("candidate")
        if cand is None:
            arcs[key] = {k: v for k, v in meta.items()}
            continue
        if q_active.get(cand, 0) == 1:
            arcs[key] = {k: v for k, v in meta.items() if k != "candidate"}

    severed = _prune_dead_units(vertices, arcs, units)
    topo: dict[str, Any] = {"vertices": vertices, "arcs": arcs, "units": units}
    if severed:
        # pruning が「生きた流れ」の行き先を巻き添え削除した（例: dead unit の outlet 頂点へ
        # 向かうリサイクル候補が ON）。この q 組合せは遺伝子型と実構造が食い違うため、
        # is_buildable が理由付きで弾けるようマークして返す（GA/BO は BAD_VALUE 経路に落とす）。
        topo["pruning_severed"] = severed
    return topo


def _prune_dead_units(
    vertices: dict[str, Any],
    arcs: dict[tuple[str, str], dict[str, Any]],
    units: dict[str, Any],
) -> list[tuple[str, str]]:
    """給餌の消えた unit を fixpoint まで連鎖除去する（具体トポロジーを in-place で刈る）。

    候補アーク（バイパストグル等）を OFF にすると膜 inlet への給餌が消える。その膜は
    実際には建たないので、具体トポロジーから取り除いて「膜オフ＝クリーンな下位構造」に
    する。これにより GA は「膜あり ⇄ 膜なし」を公平に評価できる（給餌ゼロ膜が常に
    ペナルティ域に落ちるのを防ぐ）。

    発火条件: unit の inlet が feed から到達不能（入次数0 を包含する）。
        入次数だけを見ると「給餌 OFF ＋ 自ユニット下流からのリサイクル ON」で
        feed から切り離された自己循環の島（流量ゼロの膜）が刈り残され、そのまま
        Aspen に渡って評価を浪費する。到達性で判定することでこの島も刈れる。
        feed 頂点が無い（テスト用等の）トポロジーでは旧来の入次数0 判定に落とす。
    除去内容:
        - その unit を units から削除（auto_vps が対応 VP を生成しなくなる＝自動連動）
        - 各 outlet 頂点を、それに触れる全アークごと削除（delete_unit の流出処理と同形。
          膜の permeate/retentate 所有アークもここで消える）
        - inlet 始点に残る所有アークも削除（冗長だが防御的）
        - role=internal かつ in==out==0 の孤立頂点を掃除（validate 項目7 と同定義）

    出口頂点の除去で下流 unit の inlet が入次数0 になり得るため、変化が無くなるまで反復する
    （直列膜 MEMB2→MEMB3 で MEMB2 を消すと MEMB3 も次パスで dead 化）。

    残渣振替はしない: 発火条件が「給餌なし」なので振替対象が存在しない（流入のある
    生きた unit を消す delete_unit とは別物）。feed/product/residue 頂点は決して消さない。

    Returns
    -------
    list[tuple[str, str]]
        「巻き添え切断」されたアークのリスト。dead unit の outlet 頂点へ向かっていた
        第三者アーク（unit 非所有）のうち、源頂点が最終トポロジーに生き残っているもの。
        空でなければ「遺伝子型はその接続 ON なのに実構造には存在しない」食い違いであり、
        呼び出し側（active_topology → is_buildable）が BAD 経路に落とす。
    """
    severed: list[tuple[str, str]] = []
    feed_vids = [v for v, d in vertices.items() if d.get("role") == "feed"]

    changed = True
    while changed:
        changed = False

        in_deg = {v: 0 for v in vertices}
        for (_frm, to) in arcs:
            if to in in_deg:
                in_deg[to] += 1
        reachable = _reachable_from(feed_vids, arcs, vertices) if feed_vids else None

        for name, u in list(units.items()):
            inlet = u.get("inlet")
            if reachable is not None:
                dead = inlet not in reachable
            else:
                dead = in_deg.get(inlet, 0) == 0
            if not dead:
                continue
            # dead unit を刈る
            del units[name]
            # 流出側: outlet 頂点とそれに触れる全アーク（delete_unit と同形）
            for ov in (u.get("outlets") or {}).values():
                for k in [k for k in arcs if ov in k]:
                    # 第三者からの流入（この unit の所有アークでない到着アーク）は
                    # 「生きた流れの切断」候補として記録する
                    if k[1] == ov and arcs[k].get("unit") != name:
                        severed.append(k)
                    del arcs[k]
                vertices.pop(ov, None)
            # inlet 始点に残る所有アーク（冗長だが防御的）
            for k in [k for k in arcs if k[0] == inlet]:
                del arcs[k]
            changed = True

        # 孤立 internal 頂点の掃除（in==out==0、feed/sink は対象外）
        in_deg2 = {v: 0 for v in vertices}
        out_deg2 = {v: 0 for v in vertices}
        for (frm, to) in arcs:
            if frm in out_deg2:
                out_deg2[frm] += 1
            if to in in_deg2:
                in_deg2[to] += 1
        for v in [
            v
            for v, vdef in vertices.items()
            if vdef.get("role") == "internal" and in_deg2[v] == 0 and out_deg2[v] == 0
        ]:
            del vertices[v]
            changed = True

    # 源頂点ごと消えた切断（島の内部アーク等）は「生きた流れ」ではないので除外
    return [k for k in severed if k[0] in vertices]


def is_buildable(topology: dict[str, Any]) -> str | None:
    """具体トポロジー（q 解決後）が aspen_builder でビルド可能かを判定する純関数。

    ビルド不能なら理由文字列、ビルド可能なら None を返す（Aspen 不要・副作用なし）。

    検査するのは**非膜の分流のみ**：
        builder は膜以外の分流（スプリッタ）を作れない（FSplit は未実装）ため、
        非膜頂点の out 次数 >1 を弾く。多出力が許されるのは複数出口を持つユニット
        （現状は膜）の inlet のときだけで、そのとき out 次数＝ユニットの出口数
        （len(outlets)）かつ全出アークが当該ユニット所有（meta["unit"]==unit名）
        であることを要求する。

    検査しないもの：
        - 行き止まり：builder が terminal（ストリーム未生成）／未測定出力として
          処理するためビルド可能。ここでは弾かない。
        - in 次数の合流：builder が Mixer を自動生成するためビルド可能。

    `validate` には足さない：SS テンプレは相互排他な候補ペアで out 次数2でも正常
    であり、本関数は q 解決後の具体トポロジーにのみ効く（層が異なる）。
    """
    # pruning が生きた流れを巻き添え切断した q 組合せ（active_topology がマークする）。
    # 遺伝子型（例: リサイクル ON）と実構造が食い違うため、評価せず BAD で弾く。
    severed = topology.get("pruning_severed")
    if severed:
        return f"pruning severed live arc(s) into removed unit outlet: {severed}"

    arcs = topology["arcs"]
    units = topology["units"]
    # ユニット inlet → (ユニット名, 出口数)
    inlet_of = {u["inlet"]: (name, len(u["outlets"])) for name, u in units.items()}

    for v in topology["vertices"]:
        out = [meta for (frm, _to), meta in arcs.items() if frm == v]
        if v in inlet_of:
            uname, n_out = inlet_of[v]
            if len(out) != n_out:
                return f"unit {uname} inlet {v}: out-degree {len(out)} != {n_out}"
            if any(meta.get("unit") != uname for meta in out):
                return f"unit {uname} inlet {v}: has non-{uname} outgoing arc"
        elif len(out) > 1:
            return (
                f"vertex {v}: out-degree {len(out)} but not a unit inlet "
                f"(split unbuildable)"
            )
    return None


# =========================================================
# Validate
# =========================================================

def validate(topology: dict[str, Any]) -> None:
    """SS テンプレートまたは具体トポロジーの妥当性検査。

    違反があれば TopologyError を raise する。引数は SS でも具体トポロジー
    でもよい（candidate キーは無視して全アークを最大グラフとして扱う）。

    検査項目:
        1. 全アークの endpoint が vertices に存在・自己ループ (from==to) 禁止
        2. アークの unit (あれば) が units に存在
        2b. candidate ラベルは "q_<int>" 形式かつ SS 内で一意
        3. unit 所有権:
           - inlet / outlets[*] 頂点が存在
           - inlet→各 outlet の所有アークが実在し unit タグが一致（実体の完全性）
           - unit タグ付きアークは必ずその unit の inlet→outlet 対（誤タグ禁止）
           - 所有アークは candidate 化不可（装置の内部構造は q で消えない）
           - inlet 頂点・outlet 頂点は unit 間で共有不可（同ロール間のみ。
             「A の outlet ＝ B の inlet」の直列連結は正常なので許す）
        4. role=feed の入次数 == 0
        5. role=product/residue の出次数 == 0
        6. feed → product の到達可能性（少なくとも1経路）
        7. 役割なし内部頂点で入次数+出次数 == 0 は孤立として禁止
        8. feed から到達可能な internal 頂点は何らかの sink にも到達可能
           （孤立部分グラフ・行き止まりを禁止）
        9. 不明な role を持つ頂点はない
    """
    vertices = topology["vertices"]
    arcs = topology["arcs"]
    units = topology.get("units", {})

    # 1. endpoint + 自己ループ禁止
    for (frm, to) in arcs:
        if frm not in vertices:
            raise TopologyError(f"arc references unknown vertex from={frm!r}")
        if to not in vertices:
            raise TopologyError(f"arc references unknown vertex to={to!r}")
        if frm == to:
            raise TopologyError(f"self-loop arc {frm!r}->{to!r} is not allowed")

    # 2. arc.unit が units にある
    for key, meta in arcs.items():
        u = meta.get("unit")
        if u is not None and u not in units:
            raise TopologyError(f"arc {key} references unknown unit {u!r}")

    # 2b. candidate ラベルの形式・一意性（binary_variables まで遅延させず最終ゲートで弾く）
    seen_cands: set[str] = set()
    for key, meta in arcs.items():
        cand = meta.get("candidate")
        if cand is None:
            continue
        _qid_num(cand)  # 形式不正なら TopologyError
        if cand in seen_cands:
            raise TopologyError(f"duplicate candidate label {cand!r}")
        seen_cands.add(cand)

    # 3. unit 所有権（頂点の存在＋所有アークの実体＋同ロール間の排他性）
    inlet_owner: dict[str, str] = {}
    outlet_owner: dict[str, str] = {}
    for uname, udef in units.items():
        inlet = udef.get("inlet")
        if inlet is None:
            raise TopologyError(f"unit {uname!r} has no inlet")
        if inlet not in vertices:
            raise TopologyError(f"unit {uname!r}.inlet={inlet!r} not in vertices")
        if inlet in inlet_owner:
            raise TopologyError(
                f"units {inlet_owner[inlet]!r} and {uname!r} share inlet {inlet!r}"
            )
        inlet_owner[inlet] = uname
        for port, vid in (udef.get("outlets") or {}).items():
            if vid not in vertices:
                raise TopologyError(
                    f"unit {uname!r}.outlets[{port!r}]={vid!r} not in vertices"
                )
            if vid in outlet_owner:
                raise TopologyError(
                    f"units {outlet_owner[vid]!r} and {uname!r} share outlet vertex {vid!r}"
                )
            outlet_owner[vid] = uname
            owned = arcs.get((inlet, vid))
            if owned is None:
                raise TopologyError(
                    f"unit {uname!r} missing owned arc {(inlet, vid)} "
                    f"(port {port!r}) — unit structure is broken"
                )
            if owned.get("unit") != uname:
                raise TopologyError(
                    f"arc {(inlet, vid)} should be owned by {uname!r} "
                    f"but has unit={owned.get('unit')!r}"
                )
            if owned.get("candidate"):
                raise TopologyError(
                    f"owned arc {(inlet, vid)} of unit {uname!r} must not be a candidate"
                )
    # unit タグ付きアークは必ずその unit の inlet→outlet 対
    for key, meta in arcs.items():
        u = meta.get("unit")
        if u is None:
            continue
        udef = units[u]  # 存在は検査2で保証済み
        if key[0] != udef.get("inlet") or key[1] not in (udef.get("outlets") or {}).values():
            raise TopologyError(
                f"arc {key} is tagged unit={u!r} but is not an inlet→outlet arc of it"
            )

    # 4-5. role と次数
    in_deg, out_deg = _degree(vertices, arcs)
    known_roles = SourceRoles | SinkRoles | {"internal"}
    for vid, vdef in vertices.items():
        role = vdef.get("role")
        if role not in known_roles:
            raise TopologyError(f"vertex {vid!r} has unknown role {role!r}")
        if role in SourceRoles and in_deg[vid] != 0:
            raise TopologyError(f"source vertex {vid!r} (role={role}) has incoming arc")
        if role in SinkRoles and out_deg[vid] != 0:
            raise TopologyError(f"sink vertex {vid!r} (role={role}) has outgoing arc")

    # 6. feed → product 到達可能性
    feed_vids = [v for v, d in vertices.items() if d.get("role") == "feed"]
    product_vids = [v for v, d in vertices.items() if d.get("role") == "product"]
    sink_vids = [v for v, d in vertices.items() if d.get("role") in SinkRoles]

    reachable_from_feed: set[str] = set()
    if feed_vids:
        reachable_from_feed = _reachable_from(feed_vids, arcs, vertices)
    if feed_vids and product_vids:
        if not any(p in reachable_from_feed for p in product_vids):
            raise TopologyError("no feed→product path exists")

    # 7. 孤立 internal 頂点
    for vid, vdef in vertices.items():
        if vdef.get("role") != "internal":
            continue
        if in_deg[vid] == 0 and out_deg[vid] == 0:
            raise TopologyError(f"isolated internal vertex {vid!r} (no in/out arcs)")

    # 8. feed から到達可能な internal 頂点はいずれかの sink にも到達可能
    if feed_vids and sink_vids:
        reachable_to_sink = _reachable_to(sink_vids, arcs, vertices)
        for vid in reachable_from_feed:
            if vertices[vid].get("role") != "internal":
                continue
            if vid not in reachable_to_sink:
                raise TopologyError(
                    f"internal vertex {vid!r} reachable from feed but cannot reach any sink"
                )


def _degree(
    vertices: dict[str, Any],
    arcs: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    in_deg = {v: 0 for v in vertices}
    out_deg = {v: 0 for v in vertices}
    for (frm, to) in arcs:
        if frm in out_deg:
            out_deg[frm] += 1
        if to in in_deg:
            in_deg[to] += 1
    return in_deg, out_deg


def _reachable_from(
    starts: Iterable[str],
    arcs: dict[tuple[str, str], dict[str, Any]],
    vertices: dict[str, Any],
) -> set[str]:
    adj: dict[str, set[str]] = {v: set() for v in vertices}
    for (frm, to) in arcs:
        if frm in adj:
            adj[frm].add(to)
    reachable: set[str] = set()
    stack = list(starts)
    while stack:
        v = stack.pop()
        if v in reachable:
            continue
        reachable.add(v)
        stack.extend(adj.get(v, ()))
    return reachable


def _reachable_to(
    targets: Iterable[str],
    arcs: dict[tuple[str, str], dict[str, Any]],
    vertices: dict[str, Any],
) -> set[str]:
    """逆方向到達: targets のいずれかへ到達できる頂点の集合。"""
    radj: dict[str, set[str]] = {v: set() for v in vertices}
    for (frm, to) in arcs:
        if to in radj:
            radj[to].add(frm)
    reachable: set[str] = set()
    stack = list(targets)
    while stack:
        v = stack.pop()
        if v in reachable:
            continue
        reachable.add(v)
        stack.extend(radj.get(v, ()))
    return reachable


# =========================================================
# 補助ユニットの自動導出（具体トポロジーに対して使う）
# =========================================================

def mixer_vertices(topology: dict[str, Any]) -> set[str]:
    """Mixer を置くべき頂点集合。

    規則:
        (1) 膜アーク（membrane_permeate / membrane_retentate）の src 頂点
        (2) 入次数 2 以上の頂点
        (3) sink (role=product/residue) 頂点（測定点確保のためストリーム化する）
    """
    vertices = topology["vertices"]
    arcs = topology["arcs"]
    result: set[str] = set()

    in_deg = {v: 0 for v in vertices}
    for (frm, to), meta in arcs.items():
        if meta.get("type") in MembraneArcTypes:
            result.add(frm)
        if to in in_deg:
            in_deg[to] += 1

    for v, deg in in_deg.items():
        if deg >= 2:
            result.add(v)

    for vid, vdef in vertices.items():
        if vdef.get("role") in SinkRoles:
            result.add(vid)

    return result


def splitter_vertices(topology: dict[str, Any]) -> set[str]:
    """Splitter を置くべき頂点集合（出次数 >= 2）。"""
    out_deg: dict[str, int] = {}
    for (frm, _) in topology["arcs"]:
        out_deg[frm] = out_deg.get(frm, 0) + 1
    return {v for v, d in out_deg.items() if d >= 2}


def auto_vps(topology: dict[str, Any]) -> dict[str, str]:
    """MEMB ユニット名 → 対応する VP 名のマップ。

    VP{n} の n は MEMB{n} の数値部分をそのまま流用（欠番OK）。
    """
    result: dict[str, str] = {}
    for uname, udef in topology["units"].items():
        if udef.get("type") != "MEMB":
            continue
        if not uname.startswith("MEMB"):
            raise TopologyError(f"MEMB-typed unit has non-MEMB name: {uname!r}")
        num = uname[len("MEMB"):]
        if not num.isdigit():
            raise TopologyError(f"MEMB unit {uname!r} has no numeric suffix")
        result[uname] = f"VP{num}"
    return result


# =========================================================
# 変数導出（GA 染色体の組み立てに使う）
# =========================================================

def binary_variables(ss: dict[str, Any]) -> list[dict[str, Any]]:
    """SS テンプレートの candidate アークからバイナリ変数リストを導出。

    各要素: {"name": "q_k", "arc": (frm, to), "type": str|None, "unit": str|None}。
    並びは q ラベルの数値部分昇順（決定論的）。
    """
    seen: dict[str, dict[str, Any]] = {}
    for (frm, to), meta in ss["arcs"].items():
        q = meta.get("candidate")
        if q is None:
            continue
        if q in seen:
            raise TopologyError(f"duplicate candidate label {q!r}")
        seen[q] = {
            "name": q,
            "arc": (frm, to),
            "type": meta.get("type"),
            "unit": meta.get("unit"),
        }
    return [seen[q] for q in sorted(seen, key=_qid_num)]


def continuous_variables(
    ss: dict[str, Any],
    membrane_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SS テンプレートの units から連続変数リストを導出。

    並び:
        - 外側は units の挿入順
        - 各ユニット内は unit_registry の宣言順
        - membrane_model（12.1）が tie モードのときは共有 permeance 変数
          `MEMB_perm`（unit_param=["MEMB*", "permeance_CO2"]）を**先頭**に置く。
          先頭固定なのは、pruning 後トポロジーとの位置整列（x_for_topology）を
          膜の生存パターンに依らず保つため。段別（tie=False）は各 MEMB の変数列に
          `{unit}_perm` が付く（unit_registry.make_ga_variables）。
    各ユニットの bounds_override は make_ga_variables 内で UNIT_BOUNDS より優先される。
    membrane_model=None は従来どおり（permeance は params の固定値）。
    """
    result: list[dict[str, Any]] = []
    units = ss.get("units", {})
    if (
        membrane_model is not None
        and is_tie_mode(membrane_model)
        and any(get_unit_type(u) == "MEMB" for u in units)
    ):
        result.append({
            "name":       "MEMB_perm",
            "unit_param": ["MEMB*", "permeance_CO2"],
            "bounds":     permeance_bounds_aspen(membrane_model),
        })
    for uname, udef in units.items():
        result.extend(make_ga_variables(uname, udef, membrane_model=membrane_model))
    return result


def _cv_in_topology(cv: dict[str, Any], units: dict[str, Any]) -> bool:
    """連続変数エントリが具体トポロジー（units）に対して有効かの共通述語。

    unit 名が "*" で終わるエントリ（例 "MEMB*"＝tie された膜共有変数）は、その
    プレフィクスを持つユニットが1つでも残っていれば有効。
    """
    uname = cv["unit_param"][0]
    if uname.endswith("*"):
        prefix = uname[:-1]
        return any(u.startswith(prefix) for u in units)
    return uname in units


def cont_vars_for_topology(
    cont_vars: list[dict[str, Any]],
    topology: dict[str, Any],
) -> list[dict[str, Any]]:
    """テンプレート由来の連続変数リストを、具体トポロジーに存在する変数だけに絞る。

    `x_for_topology` と同じ述語・同じ順序で cv エントリ側を絞る（値と名前の対応が
    ずれないよう、必ず両者をセットで使う）。results.json の optimal_params から
    pruned ユニットの「評価に影響しない自由次元」を除外する用途（幽霊シグナル防止）。
    """
    units = topology.get("units", {})
    return [cv for cv in cont_vars if _cv_in_topology(cv, units)]


def x_for_topology(
    x_cont: list[float],
    cont_vars: list[dict[str, Any]],
    topology: dict[str, Any],
) -> list[float]:
    """テンプレート次元の連続値ベクトルを、具体トポロジーに存在する変数だけに絞る。

    optimizer（ga/bo/run_iteration）はテンプレート ss 由来の全連続変数の x を持つが、
    evaluator は具体トポロジー（dead-unit pruning 後）の `continuous_variables` と
    **位置 zip** する。pruning で「途中の」ユニットが消えると位置がずれ、後続ユニットに
    前のユニットの値が書き込まれる（gated unit が2つ以上あり片方だけ prune された場合に
    顕在化する整列バグ）。テンプレート順を保ったままトポロジー非存在ユニットの値を
    落とすことで、evaluator 側の zip と 1:1 に揃える。

    述語は `_cv_in_topology`（cont_vars_for_topology と共通）。
    """
    units = topology.get("units", {})
    return [
        float(val)
        for val, cv in zip(x_cont, cont_vars)
        if _cv_in_topology(cv, units)
    ]


# =========================================================
# 隣接行列（論文用）
# =========================================================

def to_matrix(topology: dict[str, Any]):
    """(adjacency_matrix, vertex_order) を返す。論文用。

    値の意味:
        0    : アークなし
        1    : 固定アーク
        "q_k": 候補アーク（candidate ラベル）

    vertex_order は vertices の挿入順（文字列ID → 行/列インデックス）。
    """
    import numpy as np  # 論文用のみ。通常パスでは読み込まない
    vertex_order = list(topology["vertices"])
    idx = {v: i for i, v in enumerate(vertex_order)}
    n = len(vertex_order)
    matrix = np.zeros((n, n), dtype=object)
    for (frm, to), meta in topology["arcs"].items():
        if frm not in idx or to not in idx:
            continue
        cand = meta.get("candidate")
        matrix[idx[frm], idx[to]] = cand if cand else 1
    return matrix, vertex_order