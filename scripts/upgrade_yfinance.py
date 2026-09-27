"""Deliberate yfinance upgrade: upgrade, rerun the health check and pytest,
print a pass/fail summary. The pin in requirements.txt is updated only when
both pass; otherwise the previous version is reinstalled.

    python scripts/upgrade_yfinance.py [VERSION]
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
PIN = re.compile(r"^yfinance==\S+$", re.MULTILINE)


def pip(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", *args], capture_output=True, text=True)


def health_ok() -> tuple[bool, str]:
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from data.health import run_health_check\n"
            "from data.yfinance_provider import YFinanceProvider\n"
            "r = run_health_check(YFinanceProvider())\n"
            "print('\\n'.join(r.failures) or 'all canaries OK'); sys.exit(0 if r.ok else 1)") % str(ROOT)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def tests_ok() -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not live"], cwd=ROOT,
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout.strip().splitlines() or ["(no output)"])[-1]


def main(argv: list[str]) -> int:
    old = metadata.version("yfinance")
    target = f"yfinance=={argv[0]}" if argv else "yfinance"
    print(f"Current yfinance {old}; installing {target} ...")
    r = pip("install", "--upgrade", target)
    if r.returncode != 0:
        print(r.stderr)
        return 1
    new = subprocess.run([sys.executable, "-c", "import yfinance; print(yfinance.__version__)"],
                         capture_output=True, text=True).stdout.strip()
    h_ok, h_msg = health_ok()
    t_ok, t_msg = tests_ok()
    print("\n=== Summary ===")
    print(f"yfinance     {old} → {new}")
    print(f"health check {'PASS' if h_ok else 'FAIL'}: {h_msg}")
    print(f"pytest       {'PASS' if t_ok else 'FAIL'}: {t_msg}")
    if h_ok and t_ok:
        REQUIREMENTS.write_text(PIN.sub(f"yfinance=={new}", REQUIREMENTS.read_text()))
        print(f"PASS — requirements.txt pinned to yfinance=={new}")
        return 0
    pip("install", f"yfinance=={old}")
    print(f"FAIL — reinstalled yfinance=={old}; the pin is unchanged. Keep using cached data "
          "and check the yfinance GitHub issues for a fix.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
