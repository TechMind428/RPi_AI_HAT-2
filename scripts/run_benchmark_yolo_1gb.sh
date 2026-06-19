#!/usr/bin/env bash
set -eu

cd ~

for run in 1 2 3; do
    output="results/1gb/yolo_640x640_run${run}_1gb.csv"
    memory_log="results/1gb/yolo_640x640_run${run}_1gb_pre_memory.txt"

    echo "=== 1GB run${run} を開始します ==="
    mkdir -p results/1gb
    {
        echo "# 1GB run${run} 実行前メモリ記録"
        date
        echo
        echo "## free -m"
        free -m
        echo
        echo "## /proc/meminfo (抜粋)"
        grep -E 'MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree' /proc/meminfo
    } > "${memory_log}"
    python3 ~/scripts/benchmark_yolo.py \
        --resolution 640x640 \
        --duration 300 \
        --output "${output}"
done
