#!/usr/bin/env python3
"""
Hailo-Ollama multimodal benchmark.

YOLO物体検出の結果をテキスト化し、`hailo-ollama` のLLMへ渡して
状況説明を生成する。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from benchmark_utils import DataSaver, SystemMonitor
    from hailo_platform_detection import HailoYoloRunner
    from hailo_ollama_client import HailoOllamaClient
except ModuleNotFoundError as exc:
    if exc.name in {"benchmark_utils", "hailo_ollama_client", "hailo_platform_detection"}:
        print(f"エラー: {exc.name}.py が見つかりません")
        print(f"期待するディレクトリ: {SCRIPT_DIR}")
        print("scripts ディレクトリ一式を同じ場所に置いたまま実行してください")
        raise SystemExit(1)
    raise

try:
    import cv2
    from picamera2 import Picamera2
except ImportError as exc:  # pragma: no cover - device dependent
    print(f"警告: 必要なライブラリが見つかりません: {exc}")
    print("このスクリプトはRaspberry Pi上で実行してください")


class MultimodalBenchmark:
    """YOLO + hailo-ollama の統合ベンチマーク"""

    class_names = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
        "truck", "boat", "traffic light", "fire hydrant", "stop sign",
        "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
        "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
        "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
        "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
        "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
        "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv",
        "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
        "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
        "scissors", "teddy bear", "hair drier", "toothbrush",
    ]

    def __init__(
        self,
        yolo_model: str,
        llm_model: str,
        resolution: Tuple[int, int],
        server_url: str,
        max_tokens: int,
        confidence: float,
    ):
        self.yolo_model_path = yolo_model
        self.llm_model = llm_model
        self.resolution = resolution
        self.max_tokens = max_tokens
        self.confidence = confidence
        self.client = HailoOllamaClient(server_url=server_url)
        self.runner = HailoYoloRunner(yolo_model, confidence=confidence)

        self.camera = Picamera2()
        config = self.camera.create_preview_configuration(
            main={"size": resolution, "format": "RGB888"}
        )
        self.camera.configure(config)
        self.camera.start()

    def capture_and_detect(self) -> Tuple[List[Dict[str, Any]], float]:
        started_at = time.time()
        frame = self.camera.capture_array()
        detections = self.runner.infer(frame)
        elapsed_ms = (time.time() - started_at) * 1000
        return detections, elapsed_ms

    def build_prompt(self, detections: List[Dict[str, Any]]) -> Tuple[str, float]:
        started_at = time.time()
        if not detections:
            prompt = (
                "カメラ映像では有意な物体が検出されませんでした。"
                "事実だけを日本語で1文にまとめてください。"
            )
        else:
            counts: Dict[str, int] = {}
            for detection in detections:
                class_id = int(detection.get("class_id", 0))
                class_name = self.class_names[class_id]
                counts[class_name] = counts.get(class_name, 0) + 1

            lines = [f"- {name}: {count}個" for name, count in sorted(counts.items())]
            prompt = (
                "以下は物体検出器の出力です。見えている内容だけを述べ、"
                "推測は避けて日本語で1文にまとめてください。\n"
                "検出結果:\n"
                f"{chr(10).join(lines)}"
            )

        return prompt, (time.time() - started_at) * 1000

    def process_once(self) -> Dict[str, Any]:
        total_started_at = time.time()
        detections, detection_ms = self.capture_and_detect()
        prompt, prompt_ms = self.build_prompt(detections)
        llm_result = self.client.chat(
            model_name=self.llm_model,
            prompt=prompt,
            max_tokens=self.max_tokens,
            temperature=0.0,
        )
        total_ms = (time.time() - total_started_at) * 1000

        return {
            "detection_count": len(detections),
            "detection_ms": round(detection_ms, 2),
            "prompt_ms": round(prompt_ms, 2),
            "llm_ttft_ms": round(llm_result.ttft_ms, 2),
            "llm_total_ms": round(llm_result.total_ms, 2),
            "llm_tokens_per_sec": round(llm_result.tokens_per_sec, 3),
            "total_ms": round(total_ms, 2),
            "prompt": prompt,
            "response_text": llm_result.response_text,
        }

    def cleanup(self) -> None:
        try:
            self.camera.stop()
        except Exception:
            pass
        try:
            self.runner.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="YOLO + hailo-ollama の統合ベンチマーク")
    parser.add_argument(
        "--yolo-model",
        default=str(Path.home() / "ai-hat-models" / "yolov8m_h10.hef"),
        help="YOLOモデルのパス（既定値は ~/ai-hat-models/yolov8m_h10.hef）",
    )
    parser.add_argument("--llm-model", required=True, help="hailo-ollamaで使用するモデル名")
    parser.add_argument("--server", default="http://localhost:8000", help="hailo-ollamaのURL")
    parser.add_argument("--resolution", default="640x640", help="入力解像度")
    parser.add_argument("--duration", type=int, default=60, help="測定時間（秒）")
    parser.add_argument("--max-tokens", type=int, default=80, help="最大生成トークン数")
    parser.add_argument("--confidence", type=float, default=0.5, help="検出閾値")
    parser.add_argument("--output-prefix", default="results/multimodal/hailo_ollama", help="出力先プレフィックス")
    args = parser.parse_args()

    width, height = [int(value) for value in args.resolution.split("x")]
    benchmark = MultimodalBenchmark(
        yolo_model=args.yolo_model,
        llm_model=args.llm_model,
        resolution=(width, height),
        server_url=args.server,
        max_tokens=args.max_tokens,
        confidence=args.confidence,
    )

    system_monitor = SystemMonitor()
    details: List[Dict[str, Any]] = []
    started_at = time.time()

    try:
        while time.time() - started_at < args.duration:
            system_before = system_monitor.measure()
            result = benchmark.process_once()
            system_after = system_monitor.measure()

            row = {
                **result,
                "memory_before_mb": round(system_before["memory_used_mb"], 2),
                "memory_after_mb": round(system_after["memory_used_mb"], 2),
                "memory_percent": round(system_after["memory_percent"], 2),
                "swap_after_mb": round(system_after["swap_used_mb"], 2),
            }
            details.append(row)

            print(
                f"検出={row['detection_count']} "
                f"det={row['detection_ms']}ms "
                f"llm={row['llm_total_ms']}ms "
                f"total={row['total_ms']}ms "
                f"mem={row['memory_percent']}%"
            )
            print(f"  応答: {row['response_text'][:100]}")
    finally:
        benchmark.cleanup()

    if not details:
        raise SystemExit("有効な測定結果がありません")

    summary = {
        "iterations": len(details),
        "duration_sec": round(time.time() - started_at, 2),
        "mean_detection_ms": round(float(np.mean([row["detection_ms"] for row in details])), 2),
        "mean_llm_ttft_ms": round(float(np.mean([row["llm_ttft_ms"] for row in details])), 2),
        "mean_llm_total_ms": round(float(np.mean([row["llm_total_ms"] for row in details])), 2),
        "mean_total_ms": round(float(np.mean([row["total_ms"] for row in details])), 2),
        "p95_total_ms": round(float(np.percentile([row["total_ms"] for row in details], 95)), 2),
        "mean_tokens_per_sec": round(float(np.mean([row["llm_tokens_per_sec"] for row in details])), 3),
        "mean_memory_percent": round(system_monitor.get_statistics()["memory_percent"]["mean"], 2),
        "max_memory_percent": round(system_monitor.get_statistics()["memory_percent"]["max"], 2),
        "llm_model": args.llm_model,
        "yolo_model": args.yolo_model,
    }

    print("\n=== サマリー ===")
    for key, value in summary.items():
        print(f"{key}: {value}")

    DataSaver.save_to_csv(details, f"{args.output_prefix}_details.csv")
    DataSaver.save_to_json({"summary": summary, "details": details}, f"{args.output_prefix}.json")


if __name__ == "__main__":
    main()
