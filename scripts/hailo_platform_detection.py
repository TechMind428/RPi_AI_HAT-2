#!/usr/bin/env python3
"""
Picamera2 + HailoRT wrapper for YOLO inference.

H10 対応 HEF の読み込みと 1 フレーム推論を扱う。
`HAILO_VDEVICE_GROUP_ID` または `HAILO_OLLAMA_VDEVICE_GROUP_ID` が設定されている場合は、
同じ group_id を使う VDevice を作り、hailo-ollama との共有実行を試せるようにする。
"""

from __future__ import annotations

import time
import os
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import cv2
import numpy as np

try:
    from hailo_platform import HEF, FormatType, HailoSchedulingAlgorithm, VDevice
except ImportError as exc:  # pragma: no cover - Raspberry Pi 実機用
    raise ImportError(
        "hailo_platform を読み込めません。"
        " Raspberry Pi OS 上で python3-hailort / HailoRT Python wheel が利用できることを確認してください。"
    ) from exc


class HailoRuntimeRunner:
    """Picamera2 の Hailo ラッパー相当の処理に、VDevice group_id 指定を加えた実行器"""

    TARGET = None
    TARGET_REF_COUNT = 0
    TARGET_GROUP_ID = None

    def __init__(self, hef_path: str, batch_size: int | None = None, output_type: str = "FLOAT32"):
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN

        group_id = os.environ.get("HAILO_VDEVICE_GROUP_ID") or os.environ.get("HAILO_OLLAMA_VDEVICE_GROUP_ID")
        if group_id and hasattr(params, "group_id"):
            params.group_id = group_id
            print(f"Hailo VDevice group_id: {group_id}")

        self.batch_size = batch_size
        self.hef = HEF(hef_path)

        if HailoRuntimeRunner.TARGET is None or HailoRuntimeRunner.TARGET_GROUP_ID != group_id:
            HailoRuntimeRunner.TARGET = VDevice(params)
            HailoRuntimeRunner.TARGET_GROUP_ID = group_id

        HailoRuntimeRunner.TARGET_REF_COUNT += 1
        self.target = HailoRuntimeRunner.TARGET
        self.infer_model = self.target.create_infer_model(hef_path)
        self.infer_model.set_batch_size(1 if batch_size is None else batch_size)
        self._set_input_output(output_type)
        self.input_vstream_info, self.output_vstream_info = self._get_vstream_info()
        self.configured_infer_model = self.infer_model.configure()

    def _set_input_output(self, output_type: str) -> None:
        input_format_type = self.hef.get_input_vstream_infos()[0].format.type
        self.infer_model.input().set_format_type(input_format_type)
        output_format_type = getattr(FormatType, output_type)
        for output in self.infer_model.outputs:
            output.set_format_type(output_format_type)
        self.num_outputs = len(self.infer_model.outputs)

    def _get_vstream_info(self):
        return self.hef.get_input_vstream_infos(), self.hef.get_output_vstream_infos()

    def get_input_shape(self):
        return self.input_vstream_info[0].shape

    def _create_bindings(self):
        output_buffers = {
            name: np.empty(self.infer_model.output(name).shape, dtype=np.float32)
            for name in self.infer_model.output_names
        }
        return self.configured_infer_model.create_bindings(output_buffers=output_buffers)

    def callback(self, completion_info, bindings, future, last) -> None:
        if future._has_had_error:
            return
        if completion_info.exception:
            future._has_had_error = True
            future.set_exception(completion_info.exception)
            return

        if self.num_outputs <= 1:
            if self.batch_size is None:
                future._intermediate_result = bindings.output().get_buffer()
            else:
                future._intermediate_result.append(bindings.output().get_buffer())
        else:
            if self.batch_size is None:
                for name in bindings._output_names:
                    future._intermediate_result[name] = bindings.output(name).get_buffer()
            else:
                for name in bindings._output_names:
                    future._intermediate_result[name].append(bindings.output(name).get_buffer())

        if last:
            future.set_result(future._intermediate_result)

    def run_async(self, input_data):
        if self.batch_size is None:
            input_data = np.expand_dims(input_data, axis=0)

        future = Future()
        future._has_had_error = False
        if self.num_outputs <= 1:
            future._intermediate_result = []
        else:
            future._intermediate_result = {output.name: [] for output in self.infer_model.outputs}

        for index, frame in enumerate(input_data):
            last = index == len(input_data) - 1
            bindings = self._create_bindings()
            bindings.input().set_buffer(frame)
            self.configured_infer_model.wait_for_async_ready(timeout_ms=10000)
            self.configured_infer_model.run_async(
                [bindings],
                partial(self.callback, bindings=bindings, future=future, last=last),
            )
        return future

    def run(self, input_data):
        return self.run_async(input_data).result()

    def close(self) -> None:
        if hasattr(self, "configured_infer_model"):
            del self.configured_infer_model
        HailoRuntimeRunner.TARGET_REF_COUNT -= 1
        if HailoRuntimeRunner.TARGET_REF_COUNT == 0 and HailoRuntimeRunner.TARGET is not None:
            self.target.release()
            HailoRuntimeRunner.TARGET = None
            HailoRuntimeRunner.TARGET_GROUP_ID = None


class HailoYoloRunner:
    """HailoRT Python APIで YOLO HEF を実行する薄いラッパー"""

    def __init__(
        self,
        model_path: str,
        confidence: float = 0.5,
        profile_stages: bool = False,
    ):
        self.model_path = str(Path(model_path).expanduser())
        self.confidence = confidence
        self.profile_stages = profile_stages
        self.last_profile: Dict[str, Any] = {}

        hef_path = Path(self.model_path)
        if not hef_path.exists():
            raise FileNotFoundError(f"HEF ファイルが見つかりません: {hef_path}")

        self.hailo = HailoRuntimeRunner(str(hef_path))
        input_shape = self.hailo.get_input_shape()
        if len(input_shape) < 2:
            raise RuntimeError(f"想定外の入力 shape です: {input_shape}")

        self.input_height = int(input_shape[0])
        self.input_width = int(input_shape[1])
        self.resize_count = 0
        self.no_resize_count = 0

    @property
    def input_resolution(self) -> Tuple[int, int]:
        return self.input_width, self.input_height

    def infer(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        if self.profile_stages:
            return self._infer_with_profile(frame)

        frame_height, frame_width = frame.shape[:2]
        if frame_width == self.input_width and frame_height == self.input_height:
            model_input = frame
            self.no_resize_count += 1
        else:
            model_input = cv2.resize(frame, (self.input_width, self.input_height))
            self.resize_count += 1

        output_data = self.hailo.run(model_input)
        return self._parse_detections(output_data, frame.shape[1], frame.shape[0])

    def _infer_with_profile(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        frame_height, frame_width = frame.shape[:2]
        total_start = time.perf_counter()

        resize_start = time.perf_counter()
        resized = frame_width != self.input_width or frame_height != self.input_height
        if resized:
            model_input = cv2.resize(frame, (self.input_width, self.input_height))
            self.resize_count += 1
        else:
            model_input = frame
            self.no_resize_count += 1
        resize_ms = (time.perf_counter() - resize_start) * 1000

        hailo_start = time.perf_counter()
        output_data = self.hailo.run(model_input)
        hailo_run_ms = (time.perf_counter() - hailo_start) * 1000

        parse_start = time.perf_counter()
        detections = self._parse_detections(output_data, frame.shape[1], frame.shape[0])
        parse_ms = (time.perf_counter() - parse_start) * 1000

        self.last_profile = {
            "resize_ms": resize_ms,
            "hailo_run_ms": hailo_run_ms,
            "parse_ms": parse_ms,
            "infer_total_ms": (time.perf_counter() - total_start) * 1000,
            "resized": resized,
        }
        return detections

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
            self.hailo = None
