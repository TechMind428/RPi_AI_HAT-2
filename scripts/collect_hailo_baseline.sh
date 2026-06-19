#!/usr/bin/env bash
set -eu

OUT_DIR="${1:-results/env_checks/$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

run_and_save() {
    local name="$1"
    shift
    {
        echo "# command: $*"
        echo
        "$@"
    } >"$OUT_DIR/${name}.txt" 2>&1 || true
}

echo "保存先: $OUT_DIR"

run_and_save date date
run_and_save uname uname -a
run_and_save os_release cat /etc/os-release
run_and_save free free -h
run_and_save swaps cat /proc/swaps
run_and_save df df -h
run_and_save lspci_hailo sh -c "lspci | grep -i Hailo"
run_and_save hailo_fw_identify hailortcli fw-control identify
run_and_save cameras rpicam-hello --list-cameras
run_and_save apt_hailo sh -c "apt list --installed 2>/dev/null | grep -E 'hailo|hailort|gen-ai'"
run_and_save dpkg_hailo_models sh -c "dpkg -L hailo-models 2>/dev/null | grep -E '_h10\\.hef$' | sort"
run_and_save dpkg_hailo_ollama sh -c "dpkg -L hailo-gen-ai-model-zoo 2>/dev/null | grep -E 'hailo-ollama|manifest|models'"
run_and_save hailo_ollama_config cat /etc/xdg/hailo-ollama/hailo-ollama.json
run_and_save hailo_model_list sh -c "curl -sS http://localhost:8000/hailo/v1/list; echo"
run_and_save git_status git status --short
run_and_save git_head git rev-parse HEAD

echo "採取完了: $OUT_DIR"
