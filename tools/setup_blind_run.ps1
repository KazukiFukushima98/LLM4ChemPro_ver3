<#
.SYNOPSIS
  run 間ブラインドの「物理的隔離」（docs/blind_isolation_protocol.md）に従い、
  SST 再実行専用のクリーンなワークスペースを 1 本ぶん作る。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\setup_blind_run.ps1 -RunName rb01
  powershell -ExecutionPolicy Bypass -File tools\setup_blind_run.ps1 -RunName rb02
  powershell -ExecutionPolicy Bypass -File tools\setup_blind_run.ps1 -RestoreMemory   # 2 本とも済んだ後

.NOTES
  実行機（Aspen COM のため RDP セッション内）で、run を始める前に 1 回ずつ走らせる。
  実行機には pwsh が無いので Windows PowerShell 5.1 で動くように書いてある。
  メモリの退避は -SkipMemory を付けない限りこの script が行う。
#>
[CmdletBinding()]
param(
  [string]$RunName,
  [string]$Repo,
  [string]$Dest,
  [switch]$SkipMemory,
  [switch]$RestoreMemory
)

$ErrorActionPreference = "Stop"
$projects = Join-Path $env:USERPROFILE ".claude\projects"
$userSettings = Join-Path $env:USERPROFILE ".claude\settings.json"
$settingsBak = "$userSettings.bak_HOLD"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
# 退避先は .claude の外に置く（.claude 配下に改名して置いた退避が 1 件消えた: 2026-09-10、原因不明）。
# 退避したプロジェクト名は manifest に残し、復元時に照合する。
$holdRoot = Join-Path $env:USERPROFILE "claude_memory_hold"
$manifest = Join-Path $holdRoot "manifest.txt"
# PowerShell 5.1 では param の既定値の中で $PSScriptRoot が空になるので本体で決める。
if (-not $Repo) { $Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }

# --- 0. 後始末モード: 退避したメモリと設定を全部戻して終わる -------------------
if ($RestoreMemory) {
  $n = 0; $missing = @()
  if (Test-Path $manifest) {
    foreach ($name in (Get-Content $manifest | Where-Object { $_.Trim() })) {
      $src = Join-Path (Join-Path $holdRoot $name) "memory"
      $orig = Join-Path (Join-Path $projects $name) "memory"
      if (-not (Test-Path $src)) { $missing += $name; continue }
      if (Test-Path $orig) { Write-Host "  スキップ（memory が既にある）: $orig" -ForegroundColor Yellow; continue }
      New-Item -ItemType Directory (Join-Path $projects $name) -Force | Out-Null
      Move-Item $src $orig
      Write-Host "  復元: $orig"; $n++
    }
  }
  # 旧方式（.claude 配下で memory_HOLD_* に改名）の残りも戻す
  Get-ChildItem $projects -Directory -Recurse -Depth 1 -Filter "memory_HOLD_*" -ErrorAction SilentlyContinue |
    ForEach-Object {
      $orig = Join-Path $_.Parent.FullName "memory"
      if (Test-Path $orig) { Write-Host "  スキップ（memory が既にある）: $($_.FullName)" -ForegroundColor Yellow }
      else { Rename-Item $_.FullName $orig; Write-Host "  復元(旧方式): $orig"; $n++ }
    }
  Write-Host "memory を $n 個復元した。"
  if ($missing) { Write-Host "  警告: manifest にあるのに退避先に無い: $($missing -join ', ')" -ForegroundColor Red }
  else {
    if (Test-Path $manifest) { Remove-Item $manifest }
    if ((Test-Path $holdRoot) -and -not (Get-ChildItem $holdRoot -Recurse -File)) { Remove-Item $holdRoot -Recurse -Force }
  }
  if (Test-Path $settingsBak) {
    if ((Get-Item $settingsBak).Length -eq 0) { Remove-Item $userSettings -ErrorAction SilentlyContinue }
    else { Copy-Item $settingsBak $userSettings -Force }
    Remove-Item $settingsBak
    Write-Host "settings.json を元に戻した（autoMemoryEnabled の変更を取り消し）。"
  }
  exit 0
}
if (-not $RunName) { throw "-RunName を指定すること（例 rb01）。" }
if (-not $Dest) { $Dest = Join-Path (Split-Path -Parent $Repo) ("workspace_blind_" + $RunName) }

Write-Host "== 物理的隔離ワークスペース : $RunName ==" -ForegroundColor Cyan
Write-Host "  repo : $Repo"
Write-Host "  dest : $Dest"

# --- 1. 永続メモリの隔離（漏洩経路 #1） ------------------------------------
# 仕組み: auto memory は ~/.claude/projects/<project>/memory/ にあり、<project> は cwd ではなく
# git リポジトリのルートで決まる（公式ドキュメント）。run34 の cwd は …\ver3\algorithm だったが
# git ルートは …\ver3 なので、…-ver3\memory\MEMORY.md が毎セッション自動で読み込まれていた。
# 手当ては 3 層:
#   (1) ワークスペースを独立 git リポジトリにする（§3）→ 新しい空の memory から始まる
#   (2) ~/.claude/settings.json で autoMemoryEnabled=false（デスクトップアプリ起動にも効く）
#   (3) 既存 memory を全部退避（保険。2 本が終わるまで戻さない）
if (-not $SkipMemory) {
  # (2) 設定で OFF。元の settings.json は .bak_HOLD に保存し、-RestoreMemory で戻す
  if (-not (Test-Path $settingsBak)) {
    if (Test-Path $userSettings) { Copy-Item $userSettings $settingsBak }
    else { [System.IO.File]::WriteAllText($settingsBak, "", $utf8NoBom) }
  }
  $cfg = $null
  if (Test-Path $userSettings) {
    try { $cfg = Get-Content $userSettings -Raw | ConvertFrom-Json } catch { $cfg = $null }
  }
  if ($null -eq $cfg) { $cfg = New-Object PSObject }
  $cfg | Add-Member -MemberType NoteProperty -Name autoMemoryEnabled -Value $false -Force
  # 作業フォルダ外の Read も先に塞ぐ（rb02 の 1 回目はこの確認プロンプトで止まった）
  if (-not $cfg.permissions) { $cfg | Add-Member -MemberType NoteProperty -Name permissions -Value (New-Object PSObject) -Force }
  $cfg.permissions | Add-Member -MemberType NoteProperty -Name blockReadsOutsideWorkingDirectories -Value $true -Force
  [System.IO.File]::WriteAllText($userSettings, ($cfg | ConvertTo-Json -Depth 20), $utf8NoBom)
  Write-Host "  settings: autoMemoryEnabled=false, blockReadsOutsideWorkingDirectories=true （元は $settingsBak に保存）" -ForegroundColor Yellow

  # (3) 既存 memory を .claude の外へ退避し、manifest に記録
  New-Item -ItemType Directory $holdRoot -Force | Out-Null
  $held = 0
  Get-ChildItem $projects -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $mem = Join-Path $_.FullName "memory"
    if (Test-Path $mem) {
      $dst = Join-Path (Join-Path $holdRoot $_.Name) "memory"
      if (Test-Path $dst) { Write-Host "  memory: 退避先に既にある（スキップ）: $dst" -ForegroundColor Yellow }
      else {
        New-Item -ItemType Directory (Join-Path $holdRoot $_.Name) -Force | Out-Null
        Move-Item $mem $dst
        Add-Content -Path $manifest -Value $_.Name -Encoding UTF8
        Write-Host "  memory: 退避 -> $dst" -ForegroundColor Yellow
      }
      $held++
    }
  }
  if ($held -eq 0) { Write-Host "  memory: 退避対象なし（既に空）" }
  if (Test-Path $manifest) {
    $names = Get-Content $manifest | Where-Object { $_.Trim() } | Sort-Object -Unique
    [System.IO.File]::WriteAllText($manifest, (($names -join "`r`n") + "`r`n"), $utf8NoBom)
    Write-Host "  manifest: $($names.Count) 件 ($manifest)"
  }
}
# 退避漏れの確認（-SkipMemory は「メモリに触らず検査もしない」= 開発機での動作確認専用。
# 本番の実行機では付けないこと）
if (-not $SkipMemory) {
  $live = Get-ChildItem $projects -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName "memory" } | Where-Object { Test-Path $_ }
  if ($live) { throw "メモリが残っている: $($live -join ', ')" }
} else {
  Write-Host "  memory: -SkipMemory のため未退避・未検査（本番では付けないこと）" -ForegroundColor Yellow
}

# --- 2. クリーンツリーの構築（漏洩経路 #2 #3 #4） ---------------------------
# 含めるもの: 手法の実行に要るものだけ。
# 含めないもの: .git（コミットメッセージに他 run の結果）/ docs（実験記録）/
#               runs（兄弟 run）/ algorithm2 / backup / tools / scratch。
if (Test-Path $Dest) { throw "$Dest が既にある。消すか別名を指定すること。" }
New-Item -ItemType Directory $Dest | Out-Null
New-Item -ItemType Directory (Join-Path $Dest "algorithm") | Out-Null

# ルート直下は uv の実行に要るものだけ。ARCHITECTURE.md とルート CLAUDE.md（開発者向け）は
# エージェントに読ませない（指示書 algorithm/CLAUDE.md が自己完結）。
foreach ($f in @("pyproject.toml", "uv.lock")) {
  Copy-Item (Join-Path $Repo $f) (Join-Path $Dest $f)
}
foreach ($f in @("CLAUDE.md", "case.yaml", "ss_seed.json")) {
  Copy-Item (Join-Path $Repo "algorithm/$f") (Join-Path $Dest "algorithm/$f")
}
# baseline/ は一括最適化 baseline の driver で、SST ループは import しない（確認済み）。
# かつ run_baseline.py が「SST run30 の 6.1 h に合わせた」と run30 の実測値に言及している
# 唯一の corpus 内テキストなので、持ち込まない。tests/test_baseline.py も同じ理由で外す。
foreach ($d in @("src", "tests", ".claude")) {
  $s = Join-Path $Repo "algorithm/$d"
  if (Test-Path $s) { Copy-Item $s (Join-Path $Dest "algorithm/$d") -Recurse }
}
Remove-Item (Join-Path $Dest "algorithm/tests/test_baseline.py") -ErrorAction SilentlyContinue
Get-ChildItem $Dest -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
New-Item -ItemType Directory (Join-Path $Dest "algorithm/runs") | Out-Null
New-Item -ItemType Directory (Join-Path $Dest "algorithm/scratch") | Out-Null

# --- 3. この run 専用の git（自動コミット先） -------------------------------
Push-Location $Dest
Copy-Item (Join-Path $Repo ".gitignore") (Join-Path $Dest ".gitignore")
git init -q
git add -A
git -c user.name="blind" -c user.email="blind@local" commit -q -m "physically isolated workspace for $RunName"
Pop-Location

# --- 4. 事前検査 -------------------------------------------------------------
# 検査するのは「中身の文字列」ではなく「構成が凍結 corpus と同一か」。
#
# 理由: src/ tests/ case.yaml のコメントには run2〜run28 への言及がある
# （評価回数・旧 feed 値・獲得関数の経緯など）。これらは run30 が使った版と 1 バイトも
# 違わず、Run 1 と Run 4 が見ていたものと同一である。書き換えれば清潔になるどころか
# 比較可能性が壊れる。よって corpus は凍結し、検査は
#   (a) 凍結 corpus（repo HEAD）と byte 単位で一致しているか
#   (b) corpus に無いものが紛れ込んでいないか（docs / 他 run / 元の .git）
# の 2 点に限る。実際に漏れたのはいずれも corpus の外（メモリ・git 履歴・兄弟 run）だった。
Write-Host "`n-- 事前検査 --" -ForegroundColor Cyan

# (a) コピーしたものが repo と同一か
$mismatch = @()
Get-ChildItem $Dest -Recurse -File |
  Where-Object { $_.FullName -notlike "*\.git\*" -and $_.Name -ne ".gitignore" } | ForEach-Object {
    $rel = $_.FullName.Substring($Dest.Length).TrimStart([char]92)
    $src = Join-Path $Repo $rel
    if (-not (Test-Path $src)) { $mismatch += "余分: $rel" }
    elseif ((Get-FileHash $_.FullName).Hash -ne (Get-FileHash $src).Hash) { $mismatch += "改変: $rel" }
  }
if ($mismatch) {
  $mismatch | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
  throw "ワークスペースが凍結 corpus と一致しない。"
}
Write-Host "  凍結 corpus と byte 一致 (OK)" -ForegroundColor Green

# (b) 持ち込んではいけないものが無いか
$forbidden = @()
foreach ($n in @("docs", "algorithm2", "backup", "tools", "runs")) {
  $p = Join-Path $Dest $n
  if (Test-Path $p) { $forbidden += $n }
}
if (Test-Path (Join-Path $Dest "algorithm/docs")) { $forbidden += "algorithm/docs" }
$strayRuns = Get-ChildItem (Join-Path $Dest "algorithm/runs") -ErrorAction SilentlyContinue
if ($strayRuns) { $forbidden += "algorithm/runs に既存の run: $($strayRuns.Name -join ', ')" }
# 元リポジトリの .git（コミットメッセージに他 run の結果が入っている）が来ていないか
$gitLogCount = (git -C $Dest rev-list --count HEAD)
if ([int]$gitLogCount -ne 1) { $forbidden += "git 履歴が $gitLogCount コミットある（1 のはず）" }

if ($forbidden) {
  $forbidden | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
  throw "持ち込み禁止のものがワークスペースにある。"
}
Write-Host "  docs / 他 run / 元の git 履歴なし (OK)" -ForegroundColor Green

# --- 5. 次にやること ---------------------------------------------------------
@"

次の手順:

  1. RDP セッション（session 0 では Aspen COM が 0x80080005 で全滅する）で
     新規の Claude Code セッションを開く。--continue / --resume は使わない。

       cd "$Dest\algorithm"
       claude --model claude-opus-4-8

     エージェントが読むものはすべて algorithm\ の中にある（指示書・seed・case・src）。その外は
     blockReadsOutsideWorkingDirectories でブロックされる。

  2. 起動バナーで「Opus 4.8 with xhigh effort」を確認し、Shift+Tab で **auto mode** に切り替える
     （manual のままだと複合コマンドの権限確認で止まる: rb01 の 1 回目）。
     モデル・effort・Claude Code の版・権限モードを runs\$RunName\MODEL.txt に記録する（run 開始後でよい）。

  3. ループを開始:

       /sst-loop runs/$RunName

  4. 完走したら監査（採用ゲート）:

       uv run python "$Repo\tools\audit_blind.py" <transcript.jsonl> --own-run $RunName --corpus-dir "$Dest"

     --corpus-dir は必ずこのワークスペースを指すこと（repo 全体を指すと docs/ の
     run 番号まで「正当」扱いになり、監査が骨抜きになる）。
     判定 PASS（エージェント出力 0 件）でなければ破棄してやり直す。
     transcript は %USERPROFILE%\.claude\projects\<このワークスペースのキー>\ にある。
     結果は runs\$RunName\AUDIT.txt に保存する。

  5. 2 本とも済んだら memory と settings.json を戻す:

       powershell -ExecutionPolicy Bypass -File "$Repo\tools\setup_blind_run.ps1" -RestoreMemory

"@ | Write-Host
