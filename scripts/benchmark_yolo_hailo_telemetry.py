#!/usr/bin/env python3
"""
YOLO Object Detection Benchmark with Hailo-10H Telemetry

benchmark_yolo.py の HAT 側テレメトリ測定を標準で有効にする入口です。
"""

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from benchmark_yolo import main  # noqa: E402


if "--hat-telemetry" not in sys.argv:
    sys.argv.insert(1, "--hat-telemetry")


if __name__ == "__main__":
    main()

