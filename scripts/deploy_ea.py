"""Deploy the QuantPilot EA into discovered MT5 terminals and compile it.

Steps:
  1. Scan %APPDATA%/MetaQuotes/Terminal/*/origin.txt (read-only discovery).
  2. Select terminals whose executable lives under a "MetaTrader 5" install dir.
  3. Copy mt5/Include/QuantPilot/* -> <data>/MQL5/Include/QuantPilot/
     Copy mt5/Experts/QuantPilotEA/* -> <data>/MQL5/Experts/QuantPilotEA/
  4. Compile each .mq5 with metaeditor64.exe; require 0 errors.

Usage: python scripts/deploy_ea.py [--all]   (default: deploy only to terminals
under a 'MetaTrader 5' installation directory)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "src"))
from quantpilot_agent.discovery import discover_terminals  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC_INCLUDE = ROOT / "mt5" / "Include" / "QuantPilot"
SRC_EXPERTS = ROOT / "mt5" / "Experts" / "QuantPilotEA"

COMPILE_LOG_RE = re.compile(r"(\d+) errors?,\s*(\d+) warnings?", re.IGNORECASE)


def find_metaeditor(exe_dir: Path) -> Path:
    me = exe_dir / "metaeditor64.exe"
    if not me.exists():
        me = exe_dir / "metaeditor.exe"
    if not me.exists():
        raise FileNotFoundError(f"metaeditor not found next to terminal exe: {exe_dir}")
    return me


def deploy_to(terminal, verbose: bool = True) -> bool:
    data_dir = terminal.data_dir
    exe_path = Path(terminal.exe_path)
    # origin.txt may hold the terminal executable OR just its install directory
    if exe_path.suffix.lower() != ".exe":
        exe_path = exe_path / "terminal64.exe"
    if not exe_path.exists():
        if verbose:
            print(f"[skip] terminal exe missing: {terminal.exe_path}")
        return False

    dst_include = data_dir / "MQL5" / "Include" / "QuantPilot"
    dst_experts = data_dir / "MQL5" / "Experts" / "QuantPilotEA"
    dst_include.mkdir(parents=True, exist_ok=True)
    dst_experts.mkdir(parents=True, exist_ok=True)

    copied = []
    for f in SRC_INCLUDE.glob("*.mqh"):
        shutil.copy2(f, dst_include / f.name)
        copied.append(("include", dst_include / f.name))
    for f in SRC_EXPERTS.glob("*.mq5"):
        shutil.copy2(f, dst_experts / f.name)
        copied.append(("expert", dst_experts / f.name))
    print(f"[deploy] {data_dir}: copied {len(copied)} files")

    metaeditor = find_metaeditor(exe_path.parent)
    # Only .mq5 files are compiled; includes are validated through the expert.
    # metaeditor64 on this build wants lowercase switches and unquoted paths,
    # so paths with spaces are rejected up-front.
    mql_files = [p for kind, p in copied if kind == "expert"]
    for p in list(mql_files):
        if " " in str(p):
            print(f"[error] path contains spaces, metaeditor CLI cannot handle it: {p}")
            return False
    all_ok = True
    for mql in mql_files:
        log_file = mql.with_suffix(".compile.log")
        if log_file.exists():
            log_file.unlink()
        cmd = [str(metaeditor), f"/compile:{mql}", f"/log:{log_file}"]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        errors, warnings = _parse_log(log_file)
        status = "OK" if errors == 0 else f"FAILED ({errors} errors)"
        print(f"[compile] {mql.name}: {status}" + (f", {warnings} warnings" if warnings else ""))
        _print_log_tail(log_file, errors)
        if errors != 0:
            all_ok = False
    return all_ok


def _parse_log(log_file: Path) -> tuple[int, int]:
    if not log_file.exists():
        return 999, 0
    raw = log_file.read_bytes()
    for enc in ("utf-16-le", "utf-8", "mbcs"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    matches = COMPILE_LOG_RE.findall(text)
    if not matches:
        return 999, 0
    errors, warnings = matches[-1]
    return int(errors), int(warnings)


def _print_log_tail(log_file: Path, errors: int) -> None:
    try:
        raw = log_file.read_bytes()
        for enc in ("utf-16-le", "utf-8", "mbcs"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if errors:
            for ln in lines[-15:]:
                print("        " + ln.strip())
        else:
            for ln in lines[-3:]:
                print("        " + ln.strip())
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="deploy to every discovered terminal")
    args = parser.parse_args()

    terminals = discover_terminals()
    if not terminals:
        print("[error] no MT5 terminals discovered")
        return 2

    selected = [t for t in terminals if args.all or "metatrader 5" in t.exe_path.lower()]
    if not selected:
        print("[error] no terminal under a 'MetaTrader 5' install (use --all to force)")
        for t in terminals:
            print("   found:", t.data_dir, "->", t.exe_path)
        return 2

    ok_any = False
    for t in selected:
        ok_any = deploy_to(t) or ok_any
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(main())
