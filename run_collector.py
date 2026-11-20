#!/usr/bin/env python3
"""仓库根启动器：支持 `python3 -I run_collector.py ...`。

隔离模式 (-I) 下脚本目录不会自动进入 sys.path，这里显式插入仓库根目录，
保证无论从哪个工作目录启动都能导入 collector 包。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from collector.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
