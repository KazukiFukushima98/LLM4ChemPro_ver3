# -*- coding: utf-8 -*-
"""run-blind 監査：SST エージェントのセッション記録に他 run の情報が入っていないか調べる。

    python tools/audit_blind.py <transcript.jsonl> --own-run rb01

判定
  CRITICAL : エージェント自身の出力（thinking / text / tool_use）に他 run の識別子か
             他 run の性能値らしきものが現れた。独立性が破れている。
  WARN     : tool_result（環境が見せたもの）にだけ現れた。エージェントが使ったとは
             限らないが、経路が開いていた証拠なので目視する。
  PASS     : どちらもなし。

CRITICAL が 1 件でもあれば終了コード 1。
"""
from __future__ import annotations
import argparse, glob, io, json, os, re, sys

# --- 検出パターン -----------------------------------------------------------
RE_RUN = re.compile(r"\brun\s?(\d{1,3})\b", re.I)
RE_BASELINE = re.compile(r"\bbaseline_[A-Za-z0-9_]+", re.I)
RE_COST = re.compile(r"(\d{2,4}\.\d{1,2})\s*\$\s*/\s*t", re.I)
# 「記憶から出てきた」ことを自白する言い回し
RE_RECALL = re.compile(
    r"my memory note|memory notes say|I remember|I recall|I'm recalling|"
    r"from memory|記憶|覚えて|思い出",
    re.I)

BLOCK_AGENT = ("thinking", "text", "tool_use")


def own_tokens(own):
    """自 run を指す表記のゆれを集める（rb01 / run34 いずれでも）。"""
    raw = own.strip().replace(chr(92), "/")
    s = {raw.lower()}
    s.add(raw.split("/")[-1].lower())
    return {x for x in s if x}


def blocks(path):
    """(行番号, 時刻, role, 種別, 本文) を順に返す。"""
    for i, line in enumerate(io.open(path, encoding="utf-8", errors="replace")):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        msg = j.get("message") or {}
        if not isinstance(msg, dict):
            continue
        ts = j.get("timestamp", "")
        role = msg.get("role", j.get("type", ""))
        c = msg.get("content")
        if isinstance(c, str):
            yield i, ts, role, "text", c
            continue
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "thinking":
                yield i, ts, role, "thinking", b.get("thinking") or b.get("text") or ""
            elif t == "text":
                yield i, ts, role, "text", b.get("text") or ""
            elif t == "tool_use":
                yield i, ts, role, "tool_use", json.dumps(b.get("input"), ensure_ascii=False)
            elif t == "tool_result":
                cc = b.get("content")
                yield i, ts, role, "tool_result", cc if isinstance(cc, str) else json.dumps(cc, ensure_ascii=False)


CORPUS_EXT = (".py", ".md", ".yaml", ".yml", ".json", ".txt")


def corpus_runs(corpus_dir):
    """指示書・コード・case が正当に言及している run 番号を集める。

    `case.yaml` の旧 feed 値の由来（run24-26）や `src/bo.py` の獲得関数の経緯（run21-23）は
    run30 が使った版と 1 バイトも違わない凍結物で、Run 1 / Run 4 も同じものを読んでいる。
    これらは汚染ではないので、監査の対象から外す基準をここで自動的に作る。
    """
    found = set()
    for root, dirs, files in os.walk(corpus_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "runs", "scratch")]
        for fn in files:
            if not fn.lower().endswith(CORPUS_EXT):
                continue
            try:
                t = io.open(os.path.join(root, fn), encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            found.update(m.group(1) for m in RE_RUN.finditer(t))
    return found


def scan(path, own, allowed=frozenset(), ctx=260):
    own_s = own_tokens(own)
    own_nums = {m.group(1) for tok in own_s for m in [RE_RUN.match(tok)] if m}
    crit, warn, corpus_n = [], [], 0
    for i, ts, role, kind, text in blocks(path):
        if not text:
            continue
        hits = []
        for m in RE_RUN.finditer(text):
            if m.group(1) in own_nums:
                continue
            if m.group(0).lower().replace(" ", "") in own_s:
                continue
            if m.group(1) in allowed:
                corpus_n += 1
                continue
            hits.append(("other-run", m))
        for m in RE_BASELINE.finditer(text):
            hits.append(("baseline-run", m))
        # コスト値は単体では意味がない（自分の結果が大量に出る）。
        # 同じブロックに他 run の識別子が同居しているときだけ「他 run の性能値」を疑う。
        if hits:
            for m in RE_COST.finditer(text):
                hits.append(("cost-near-other-run", m))
        if not hits:
            continue
        recalled = bool(RE_RECALL.search(text))
        for tag, m in hits:
            rec = {
                "line": i, "ts": ts, "role": role, "kind": kind, "tag": tag,
                "token": m.group(0), "recall_phrasing": recalled,
                "excerpt": text[max(0, m.start() - ctx):m.start() + ctx].replace("\n", " "),
            }
            (crit if kind in BLOCK_AGENT else warn).append(rec)
    return crit, warn, corpus_n


# 判断記録（ss_change.json の reason / HANDOFF.md）に外部知識の痕跡がないかを見る語句。
# run33 の記録にあった "a 4-membrane feasible design is known to exist in this campaign" を典型とする。
RE_EXTERNAL = re.compile(
    r"known to exist|in this campaign|previous(ly)? (run|campaign)|prior run|other run|earlier run|"
    r"another run|past run|memory note|I remember|I recall|from memory|"
    r"\brun\s?\d{1,3}\b|baseline_[A-Za-z0-9_]+",
    re.I)


def scan_records(run_dir, own):
    """run の判断記録そのもの（論文の一次記録）を走査する。戻り値は (件数, 抜粋リスト)。"""
    own_s = own_tokens(own)
    files = sorted(glob.glob(os.path.join(run_dir, "iterations", "iter_*", "ss_change.json")))
    hp = os.path.join(run_dir, "HANDOFF.md")
    if os.path.exists(hp):
        files.append(hp)
    hits = []
    for f in files:
        try:
            t = io.open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in RE_EXTERNAL.finditer(t):
            tok = m.group(0)
            if tok.lower().replace(" ", "") in own_s:
                continue
            hits.append((os.path.relpath(f, run_dir), tok,
                         t[max(0, m.start() - 100):m.end() + 100].replace("\n", " ")))
    return len(files), hits


def dedup(rows, keep=6):
    """同じ (種別, トークン) の連続を間引く。thinking は同一内容が何度も出る。"""
    seen, out = {}, []
    for r in rows:
        k = (r["kind"], r["tag"], r["token"])
        seen[k] = seen.get(k, 0) + 1
        if seen[k] <= keep:
            out.append(r)
    return out, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", nargs="*")
    ap.add_argument("--own-run", required=True, help="この run 自身の名前（例 rb01 / run34）")
    ap.add_argument("--corpus-dir", help="凍結 corpus（ワークスペース）。ここが言及する run 番号は除外する")
    ap.add_argument("--run-dir", help="判断記録（iterations/*/ss_change.json, HANDOFF.md）も走査する run ディレクトリ")
    ap.add_argument("--quiet", action="store_true", help="抜粋を出さずサマリだけ")
    a = ap.parse_args()
    if not a.transcript and not a.run_dir:
        ap.error("transcript か --run-dir の少なくとも一方を指定すること")

    if a.run_dir:
        nfiles, rhits = scan_records(a.run_dir, a.own_run)
        print("=" * 78)
        print("判断記録の監査  %s  (%d ファイル: ss_change.json × iter + HANDOFF.md)" % (a.run_dir, nfiles))
        print("判定: %s   （外部知識・他 run への言及 %d 件）" % ("CRITICAL" if rhits else "PASS", len(rhits)))
        print("=" * 78)
        for f, tok, ex in rhits:
            print("[%s] token=%s" % (f, tok))
            print("    ..." + ex + "...")
        if rhits and not a.transcript:
            return 1

    allowed = corpus_runs(a.corpus_dir) if a.corpus_dir else frozenset()
    if a.corpus_dir:
        print("corpus 由来として除外する run 番号: %s"
              % (", ".join("run" + n for n in sorted(allowed, key=int)) or "(なし)"))

    worst = 0
    for path in a.transcript:
        crit, warn, corpus_n = scan(path, a.own_run, allowed)
        crit_s, crit_n = dedup(crit)
        warn_s, warn_n = dedup(warn, keep=3)
        verdict = "CRITICAL" if crit else ("WARN" if warn else "PASS")
        worst = max(worst, 2 if crit else (1 if warn else 0))

        print("=" * 78)
        print("%s  own-run=%s" % (os.path.basename(path), a.own_run))
        print("判定: %s   （エージェント出力 %d 件 / 環境出力 %d 件）"
              % (verdict, len(crit), len(warn)))
        if a.corpus_dir:
            print("corpus 由来の言及（汚染ではない）: %d 件" % corpus_n)
        recalls = sum(1 for r in crit if r["recall_phrasing"])
        if recalls:
            print("うち「記憶から出た」言い回しを伴うもの: %d 件 ← 永続メモリ経路" % recalls)
        print("=" * 78)
        if a.quiet:
            continue
        for label, rows, counts in (("エージェント出力", crit_s, crit_n),
                                    ("環境出力", warn_s, warn_n)):
            if not rows:
                continue
            print("\n--- %s ---" % label)
            for r in rows:
                print("[%s] %s %s %s  token=%s%s"
                      % (r["kind"], r["line"], r["ts"], r["tag"], r["token"],
                         "  <RECALL>" if r["recall_phrasing"] else ""))
                print("    ..." + r["excerpt"] + "...")
            more = {k: v for k, v in counts.items() if v > (6 if label.startswith("エ") else 3)}
            if more:
                print("  （同種の重複: %s）"
                      % ", ".join("%s/%s x%d" % (k[0], k[2], v) for k, v in more.items()))
    return 1 if worst == 2 else 0


if __name__ == "__main__":
    sys.exit(main())
