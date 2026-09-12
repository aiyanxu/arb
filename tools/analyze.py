#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

The implementation lives in the entropy_arb package
(entropy_arb/analyze.py) so the installed `entropy-arb` entry point can
serve it too — this wrapper keeps the historical

    python3 tools/analyze.py            # == entropy-arb analyze

invocation working from the repo root.  See entropy_arb/analyze.py for
the full documentation (module docstring, flags, output format).

分析机器人自动采集的分钟级盘口数据并输出 thresholds 建议值；实现位于
entropy_arb/analyze.py，本脚本仅为兼容入口，等价于 `entropy-arb analyze`。
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    from entropy_arb.analyze import CANDIDATES, main, pctl  # noqa: F401
except ImportError as e:
    raise SystemExit(
        "this tool needs the entropy_arb package — run it from the repo root "
        "or pip install entropy-arb / 需在仓库根目录运行或先安装 entropy-arb") from e

if __name__ == "__main__":
    main()
