#!/usr/bin/env bash
set -euo pipefail

source "$HOME/ai-hat-env/bin/activate"

# hailo-ollama 側も同じ値で起動しておく。
export HAILO_VDEVICE_GROUP_ID="${HAILO_VDEVICE_GROUP_ID:-HAILO_OLLAMA_SHARED}"

VOICE="$(find /usr/share/hts-voice -name '*.htsvoice' | sort | head -1)"
MEM_GB="$(awk '/MemTotal/ {gb=$2/1024/1024; if (gb<2) print "1gb"; else if (gb<12) print "8gb"; else print "16gb"}' /proc/meminfo)"
RESULT_DIR="$HOME/results/chapter6"
mkdir -p "$RESULT_DIR"

python3 "$HOME/scripts/multimodal_voice_assistant.py" \
  --gpio 26 \
  --trigger-only \
  --show-prompt \
  --release-wait 0 \
  --llm-api generate \
  --llm-model qwen3:1.7b \
  --max-tokens 90 \
  --telemetry \
  --telemetry-output "$RESULT_DIR/multimodal_trigger_${MEM_GB}.csv" \
  --telemetry-jsonl "$RESULT_DIR/multimodal_trigger_${MEM_GB}.jsonl" \
  --tts-command "open_jtalk -x /var/lib/mecab/dic/open-jtalk/naist-jdic -m $VOICE -ow {wav} {text}" \
  --play-command "aplay -D plughw:2,0 {wav}"
