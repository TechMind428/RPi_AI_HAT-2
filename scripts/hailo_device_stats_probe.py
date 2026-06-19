#!/usr/bin/env python3
"""Probe Hailo-10H device telemetry exposed by pyHailoRT."""

import argparse
import json
import time

from hailo_platform import Device


def object_to_dict(obj):
    result = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception as exc:  # noqa: BLE001
            result[name] = f"<error: {exc}>"
            continue
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool, type(None))):
            result[name] = value
        else:
            result[name] = str(value)
    return result


def safe_call(label, func):
    try:
        return {"ok": True, "value": object_to_dict(func())}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main():
    parser = argparse.ArgumentParser(
        description="Hailo デバイスの温度、性能統計、電力計測可否を確認する"
    )
    parser.add_argument("--samples", type=int, default=1, help="採取回数")
    parser.add_argument("--interval", type=float, default=1.0, help="採取間隔（秒）")
    parser.add_argument(
        "--sampling-period-ms",
        type=int,
        default=100,
        help="query_performance_stats に渡すサンプリング期間（ms）",
    )
    args = parser.parse_args()

    device = Device()
    try:
        print(json.dumps({
            "identify": object_to_dict(device.control.identify()),
            "extended_device_information": object_to_dict(
                device.control.get_extended_device_information()
            ),
        }, ensure_ascii=False, indent=2))

        for index in range(args.samples):
            if index:
                time.sleep(args.interval)

            record = {
                "sample": index + 1,
                "timestamp": time.time(),
                "temperature": safe_call(
                    "temperature", device.control.get_chip_temperature
                ),
                "performance_stats": safe_call(
                    "performance_stats",
                    lambda: device.control.query_performance_stats(
                        args.sampling_period_ms
                    ),
                ),
                "single_power_measurement": safe_call(
                    "single_power_measurement", device.control.power_measurement
                ),
            }
            print(json.dumps(record, ensure_ascii=False, indent=2))
    finally:
        device.release()


if __name__ == "__main__":
    main()
