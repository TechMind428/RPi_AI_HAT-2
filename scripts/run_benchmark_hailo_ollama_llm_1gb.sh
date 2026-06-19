#!/usr/bin/env bash
set -eu

cd ~

if [ -f ~/ai-hat-env/bin/activate ]; then
    . ~/ai-hat-env/bin/activate
fi

MODELS="all"
OUTPUT_PREFIX="results/1gb/llm_compare_1gb"
MEMORY_LOG="results/1gb/llm_compare_1gb_pre_memory.txt"

mkdir -p results/1gb

{
    echo "# 1GB LLM比較 実行前メモリ記録"
    date
    echo
    echo "## free -m"
    free -m
    echo
    echo "## /proc/meminfo (抜粋)"
    grep -E 'MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree' /proc/meminfo
    echo
    echo "## 利用可能モデル"
    curl -sS http://localhost:8000/hailo/v1/list | jq -r '.models[]'
} > "${MEMORY_LOG}"

python3 ~/scripts/benchmark_hailo_ollama_llm.py \
    --models "${MODELS}" \
    --prompts ~/experiments/llm_quality_eval_prompts.jsonl \
    --output-prefix "${OUTPUT_PREFIX}" \
    --runs 3 \
    --max-tokens 128 \
    --temperature 0.0 \
    --warmup \
    --check-available
