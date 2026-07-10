#!/usr/bin/env bash
set -eu

cd ~

model="/usr/share/hailo-models/yolov8s_h8l.hef"

for run in 1 2 3; do
    output="results/aikit/yolo_640x640_run${run}_aikit.csv"
    memory_log="results/aikit/yolo_640x640_run${run}_aikit_pre_memory.txt"

    echo "=== AI Kit run${run} を開始します ==="
    mkdir -p results/aikit
    {
        echo "# AI Kit run${run} 実行前メモリ記録"
        date
        echo
        echo "## model"
        echo "${model}"
        echo
        echo "## free -m"
        free -m
        echo
        echo "## /proc/meminfo (抜粋)"
        grep -E 'MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree' /proc/meminfo
    } > "${memory_log}"
    python3 ~/scripts/benchmark_yolo_hailo_telemetry.py \
        --model "${model}" \
        --resolution 640x640 \
        --duration 300 \
        --profile-stages \
        --output "${output}"
done
