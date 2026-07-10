---
description: 最新の results.json を分析し、次の ss_change.json を提案して書く（apply/run はしない）
argument-hint: <run dir, 例: runs/run24>
---
あなたは SST エージェント（`CLAUDE.md`＝この algorithm ディレクトリの指示書）として振る舞う。提案だけ行い、適用・実行はしない。**セッションは `algorithm/` で開かれている前提**（run データは `runs/`）。

**引数の解釈**：`$ARGUMENTS` の1個目の空白区切りトークンを**対象 run ディレクトリ**（以降 **RUN**）とする。`$1` の位置指定は 0/1 始まりが環境で揺れるため使わない。空ならどの run か尋ねる。

1. `RUN/iterations/` で `results.json` を持つ最大番号の `iter_MMM/` を最新とし、その `results.json` を読む（番号を M とする。`results.json` を持たない dir は飛ばす）。
2. `CLAUDE.md` の「シグナル → 構造提案」に従ってシグナルを抽出する（境界張り付き／エネルギー支配／残渣の CO₂ ロス／不活性候補／制約違反）。**読み取った具体数値を必ず示す。**
3. 「削除＋追加のセット」の `ss_change.json` を組み立て `RUN/iterations/iter_MMM/ss_change.json` に書く。`reason` に根拠の数値を引用。リサイクルを足す場合は戻す残渣の CO₂ 流量を確認。変数総数・バイナリ数が `case.yaml` の上限内かを自己点検する。
4. **`apply_ss.py` / `run_iteration.py` は実行しない。** 提案内容と根拠を報告して停止（人間が中身を確認するためのモード）。
