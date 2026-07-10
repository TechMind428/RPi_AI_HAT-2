#!/usr/bin/env bash
set -euo pipefail

source "$HOME/ai-hat-env/bin/activate"

RESULT_DIR="$HOME/results/chapter6"
IMAGE_PATH="${1:-$HOME/vlm-test/images/test.jpg}"
PROMPT="${VLM_PROMPT:-この画像に写っているものを日本語で1文で説明してください。推測は書かないでください。}"
RUNS="${VLM_RUNS:-3}"
CSV_PATH="$RESULT_DIR/vlm_japanese_smoke.csv"
JSONL_PATH="$RESULT_DIR/vlm_japanese_smoke.jsonl"
MD_PATH="$RESULT_DIR/vlm_japanese_smoke_summary.md"

mkdir -p "$RESULT_DIR"
rm -f "$CSV_PATH" "$JSONL_PATH" "$MD_PATH"
for run in $(seq 1 "$RUNS"); do
  rm -f \
    "$RESULT_DIR/vlm_japanese_smoke_run${run}.csv" \
    "$RESULT_DIR/vlm_japanese_smoke_run${run}.jsonl" \
    "$RESULT_DIR/vlm_japanese_smoke_run${run}_summary.md"
done

echo "画像: $IMAGE_PATH"
echo "プロンプト: $PROMPT"
echo "実行回数: $RUNS"
echo "統合CSV: $CSV_PATH"
echo "統合JSONL: $JSONL_PATH"
echo "統合Markdown: $MD_PATH"

for run in $(seq 1 "$RUNS"); do
  RUN_ID="run${run}"
  RUN_CSV="$RESULT_DIR/vlm_japanese_smoke_${RUN_ID}.csv"
  RUN_JSONL="$RESULT_DIR/vlm_japanese_smoke_${RUN_ID}.jsonl"
  RUN_MD="$RESULT_DIR/vlm_japanese_smoke_${RUN_ID}_summary.md"

  echo
  echo "=== VLM日本語スモークテスト ${RUN_ID}/${RUNS} ==="
  echo "run CSV: $RUN_CSV"
  echo "run JSONL: $RUN_JSONL"

  python3 "$HOME/scripts/vlm_smoke.py" \
    --run-id "$RUN_ID" \
    --model "$HOME/hailo-vlm-models/Qwen2-VL-2B-Instruct.hef" \
    --image "$IMAGE_PATH" \
    --prompt "$PROMPT" \
    --max-tokens 96 \
    --output-csv "$CSV_PATH" \
    --output-jsonl "$JSONL_PATH"

  python3 "$HOME/scripts/vlm_smoke.py" \
    --run-id "$RUN_ID" \
    --model "$HOME/hailo-vlm-models/Qwen3-VL-2B-Instruct.hef" \
    --image "$IMAGE_PATH" \
    --prompt "$PROMPT" \
    --max-tokens 96 \
    --output-csv "$CSV_PATH" \
    --output-jsonl "$JSONL_PATH"

  awk -F, 'NR == 1 || $2 == "'"$RUN_ID"'"' "$CSV_PATH" > "$RUN_CSV"
  grep "\"run_id\": \"$RUN_ID\"" "$JSONL_PATH" > "$RUN_JSONL"

  python3 "$HOME/scripts/summarize_vlm_japanese_smoke.py" \
    --input "$RUN_CSV" \
    --output "$RUN_MD"
done

python3 "$HOME/scripts/summarize_vlm_japanese_smoke.py" \
  --input "$CSV_PATH" \
  --output "$MD_PATH"

echo
echo "保存しました:"
echo "  $CSV_PATH"
echo "  $JSONL_PATH"
echo "  $MD_PATH"
for run in $(seq 1 "$RUNS"); do
  echo "  $RESULT_DIR/vlm_japanese_smoke_run${run}.csv"
  echo "  $RESULT_DIR/vlm_japanese_smoke_run${run}.jsonl"
  echo "  $RESULT_DIR/vlm_japanese_smoke_run${run}_summary.md"
done
