#!/usr/bin/env bash
set -euo pipefail

source "$HOME/ai-hat-env/bin/activate"

python3 "$HOME/scripts/evaluate_coco80_risk_llm.py" \
  --model qwen3:1.7b \
  --runs 3 \
  --max-tokens 128 \
  --temperature 0.0 \
  --output "$HOME/results/chapter6/coco80_risk_llm_qwen3_1_7b.csv"
