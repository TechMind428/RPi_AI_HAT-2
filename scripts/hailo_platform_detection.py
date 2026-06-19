#!/usr/bin/env python3
"""
Picamera2 Hailo wrapper for YOLO inference.

Raspberry Pi OS / Picamera2 が提供する `picamera2.devices.Hailo` を使って、
H10 対応 HEF の読み込みと 1 フレーム推論を扱う。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import cv2
import numpy as np

try:
    from picamera2.devices import Hailo
except ImportError as exc:  # pragma: no cover - Raspberry Pi 実機用
    raise ImportError(
        "picamera2.devices.Hailo を読み込めません。"
        " Raspberry Pi OS 上で python3-picamera2 が利用できることを確認してください。"
    ) from exc


class HailoYoloRunner:
    """Picamera2 の Hailo ラッパーで YOLO HEF を実行する薄いラッパー"""

    def __init__(self, model_path: str, confidence: float = 0.5):
        self.model_path = str(Path(model_path).expanduser())
        self.confidence = confidence

        hef_path = Path(self.model_path)
        if not hef_path.exists():
            raise FileNotFoundError(f"HEF ファイルが見つかりません: {hef_path}")

        self.hailo = Hailo(str(hef_path))
        input_shape = self.hailo.get_input_shape()
        if len(input_shape) < 2:
            raise RuntimeError(f"想定外の入力 shape です: {input_shape}")

        self.input_height = int(input_shape[0])
        self.input_width = int(input_shape[1])

    @property
    def input_resolution(self) -> Tuple[int, int]:
        return self.input_width, self.input_height

    def infer(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        resized = cv2.resize(frame, (self.input_width, self.input_height))
        output_data = self.hailo.run(resized)
        return self._parse_detections(output_data, frame.shape[1], frame.shape[0])

    def _parse_detections(
        self,
        output_data: Any,
        frame_width: int,
        frame_height: int,
    ) -> List[Dict[str, Any]]:
        detections: List[Dict[str, Any]] = []

        if isinstance(output_data, dict):
            output_iterable: Iterable = output_data.values()
        else:
            output_iterable = output_data

        for class_id, class_detections in enumerate(output_iterable):
            for detection in class_detections:
                if len(detection) < 5:
                    continue

                confidence = float(detection[4])
                if confidence < self.confidence:
                    continue

                ymin, xmin, ymax, xmax = [float(value) for value in detection[:4]]
                x1 = max(0, min(frame_width - 1, int(xmin * frame_width)))
                y1 = max(0, min(frame_height - 1, int(ymin * frame_height)))
                x2 = max(0, min(frame_width - 1, int(xmax * frame_width)))
                y2 = max(0, min(frame_height - 1, int(ymax * frame_height)))

                detections.append(
                    {
                        "class_id": class_id,
                        "confidence": confidence,
                        "bbox": [x1, y1, x2, y2],
                    }
                )

        return detections

    def close(self) -> None:
        if hasattr(self, "hailo") and self.hailo is not None:
            self.hailo.close()
            self.hailo = None
