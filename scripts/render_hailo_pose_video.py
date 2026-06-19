#!/usr/bin/env python3
"""
Render a pose overlay video with a Hailo YOLOv8 pose HEF.

This script is intended for Raspberry Pi + AI HAT+ 2. It reads frames from an
input movie, runs a H10 pose HEF with picamera2.devices.Hailo, draws COCO-style
17 keypoints and skeleton lines, and writes an MP4 file.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

np = None


DEFAULT_MODEL = "/usr/share/hailo-models/yolov8s_pose_h10.hef"
DEFAULT_INPUT = "AI HAT+2.mp4"
COCO_KEYPOINTS = [
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
]

COCO_SKELETON = [
    (5, 7),
    (7, 9),
    (6, 8),
    (8, 10),
    (5, 6),
    (5, 11),
    (6, 12),
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (0, 1),
    (0, 2),
    (1, 3),
    (2, 4),
]


def load_runtime_dependencies():
    try:
        import numpy as numpy_module
    except ModuleNotFoundError as exc:  # pragma: no cover - Raspberry Pi 実機用
        print("エラー: Python モジュール 'numpy' が見つかりません")
        print("Raspberry Pi の仮想環境で実行する場合は、第3章の Python 環境を確認してください。")
        raise SystemExit(1) from exc

    try:
        import cv2
    except ModuleNotFoundError as exc:
        print("エラー: Python モジュール 'cv2' が見つかりません")
        print("Raspberry Pi OS 側では、通常 python3-opencv パッケージを使います。")
        raise SystemExit(1) from exc

    try:
        from picamera2.devices import Hailo
    except ImportError as exc:  # pragma: no cover - Raspberry Pi 実機用
        print("エラー: picamera2.devices.Hailo を読み込めません")
        print("Raspberry Pi OS 上で python3-picamera2 が使える環境で実行してください。")
        raise SystemExit(1) from exc

    return cv2, Hailo, numpy_module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hailo YOLOv8 pose HEF でPOSEの点と線を重ねた動画を作成します",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help="入力動画ファイル")
    parser.add_argument(
        "--output",
        default=None,
        help="出力動画ファイル。省略時は入力ファイル名に _hailo_pose を付けます",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="pose HEF ファイル")
    parser.add_argument("--confidence", type=float, default=0.5, help="人物検出の信頼度しきい値")
    parser.add_argument(
        "--keypoint-confidence",
        type=float,
        default=0.3,
        help="キーポイント描画の信頼度しきい値",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="処理する最大フレーム数。短時間確認用",
    )
    parser.add_argument(
        "--dump-output",
        action="store_true",
        help="最初の推論出力の型とshapeを表示します",
    )
    parser.add_argument("--nms-iou", type=float, default=0.45, help="NMS の IoU しきい値")
    parser.add_argument("--max-candidates", type=int, default=200, help="NMS 前に残す最大候補数")
    parser.add_argument("--max-poses", type=int, default=8, help="1フレームに描画する最大POSE数")
    return parser.parse_args()


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_hailo_pose.mp4")


def describe_output(output_data: Any, prefix: str = "output") -> None:
    if isinstance(output_data, dict):
        print(f"{prefix}: dict keys={list(output_data.keys())}")
        for key, value in output_data.items():
            describe_output(value, f"{prefix}[{key!r}]")
        return

    if isinstance(output_data, (list, tuple)):
        print(f"{prefix}: {type(output_data).__name__} len={len(output_data)}")
        for index, value in enumerate(output_data[:8]):
            describe_output(value, f"{prefix}[{index}]")
        return

    array = np.asarray(output_data)
    print(f"{prefix}: {type(output_data).__name__} shape={array.shape} dtype={array.dtype}")
    if array.size:
        flat = array.reshape(-1)
        preview = flat[: min(12, flat.size)]
        print(f"{prefix}: preview={preview}")


def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-values))


def softmax(values: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = values - np.max(values, axis=axis, keepdims=True)
    exp_values = np.exp(shifted)
    return exp_values / np.sum(exp_values, axis=axis, keepdims=True)


def normalize_scores(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values
    if np.nanmin(values) >= 0.0 and np.nanmax(values) <= 1.0:
        return values
    return sigmoid(values)


def box_iou(box: Sequence[float], boxes: np.ndarray) -> np.ndarray:
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)

    box_area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
    boxes_area = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(
        0.0, boxes[:, 3] - boxes[:, 1]
    )
    union = box_area + boxes_area - inter
    return inter / np.maximum(union, 1e-6)


def nms(poses: List[Dict[str, Any]], iou_threshold: float, max_poses: int) -> List[Dict[str, Any]]:
    if not poses:
        return []

    poses = sorted(poses, key=lambda item: item["confidence"], reverse=True)
    kept: List[Dict[str, Any]] = []

    while poses and len(kept) < max_poses:
        current = poses.pop(0)
        kept.append(current)

        if not poses:
            break

        boxes = np.array([pose["bbox"] for pose in poses], dtype=float)
        ious = box_iou(current["bbox"], boxes)
        poses = [pose for pose, iou in zip(poses, ious) if iou < iou_threshold]

    return kept


class HailoPoseRunner:
    def __init__(
        self,
        hailo_class,
        cv2_module,
        model_path: str,
        confidence: float,
        keypoint_confidence: float,
        nms_iou: float,
        max_candidates: int,
        max_poses: int,
    ):
        self.cv2 = cv2_module
        self.model_path = str(Path(model_path).expanduser())
        self.confidence = confidence
        self.keypoint_confidence = keypoint_confidence
        self.nms_iou = nms_iou
        self.max_candidates = max_candidates
        self.max_poses = max_poses

        hef_path = Path(self.model_path)
        if not hef_path.exists():
            raise FileNotFoundError(f"HEF ファイルが見つかりません: {hef_path}")

        self.hailo = hailo_class(str(hef_path))
        input_shape = self.hailo.get_input_shape()
        if len(input_shape) < 2:
            raise RuntimeError(f"想定外の入力 shape です: {input_shape}")

        self.input_height = int(input_shape[0])
        self.input_width = int(input_shape[1])

    def infer(self, frame: np.ndarray, dump_output: bool = False) -> List[Dict[str, Any]]:
        frame_height, frame_width = frame.shape[:2]
        resized = self.cv2.resize(frame, (self.input_width, self.input_height))
        output_data = self.hailo.run(resized)

        if dump_output:
            describe_output(output_data)

        return self._parse_pose(output_data, frame_width, frame_height)

    def _parse_pose(self, output_data: Any, frame_width: int, frame_height: int) -> List[Dict[str, Any]]:
        if not isinstance(output_data, dict):
            raise RuntimeError("想定外の Hailo 出力です。--dump-output の結果を確認してください。")

        candidates: List[Dict[str, Any]] = []
        for grid_size in (80, 40, 20):
            bbox = self._find_output(output_data, grid_size, 64)
            score = self._find_output(output_data, grid_size, 1)
            keypoints = self._find_output(output_data, grid_size, 51)
            if bbox is None or score is None or keypoints is None:
                continue

            candidates.extend(
                self._decode_scale(
                    bbox,
                    score,
                    keypoints,
                    frame_width,
                    frame_height,
                )
            )

        if not candidates:
            return []

        candidates = sorted(candidates, key=lambda item: item["confidence"], reverse=True)[
            : self.max_candidates
        ]
        return nms(candidates, self.nms_iou, self.max_poses)

    @staticmethod
    def _find_output(output_data: Dict[str, Any], grid_size: int, channels: int) -> np.ndarray | None:
        for value in output_data.values():
            array = np.asarray(value)
            if array.shape == (grid_size, grid_size, channels):
                return array.astype(float)
        return None

    def _decode_scale(
        self,
        bbox_output: np.ndarray,
        score_output: np.ndarray,
        keypoint_output: np.ndarray,
        frame_width: int,
        frame_height: int,
    ) -> List[Dict[str, Any]]:
        grid_h, grid_w = score_output.shape[:2]
        stride_x = self.input_width / grid_w
        stride_y = self.input_height / grid_h

        scores = normalize_scores(score_output[..., 0])
        ys, xs = np.where(scores >= self.confidence)
        if len(xs) == 0:
            return []

        # Keep only the strongest cells before the heavier box/keypoint decode.
        order = np.argsort(scores[ys, xs])[::-1][: self.max_candidates]
        xs = xs[order]
        ys = ys[order]

        reg_max = bbox_output.shape[-1] // 4
        projection = np.arange(reg_max, dtype=float)
        scale_x = frame_width / self.input_width
        scale_y = frame_height / self.input_height

        poses: List[Dict[str, Any]] = []
        for x_index, y_index in zip(xs, ys):
            confidence = float(scores[y_index, x_index])
            dfl = bbox_output[y_index, x_index].reshape(4, reg_max)
            distances = (softmax(dfl, axis=1) * projection).sum(axis=1)

            center_x = (x_index + 0.5) * stride_x
            center_y = (y_index + 0.5) * stride_y
            left, top, right, bottom = distances
            x1 = (center_x - left * stride_x) * scale_x
            y1 = (center_y - top * stride_y) * scale_y
            x2 = (center_x + right * stride_x) * scale_x
            y2 = (center_y + bottom * stride_y) * scale_y

            raw_keypoints = keypoint_output[y_index, x_index].reshape(17, 3)
            keypoints = []
            for index, (raw_x, raw_y, raw_score) in enumerate(raw_keypoints):
                # YOLOv8 pose head predicts offsets relative to the grid cell.
                kpt_x = ((raw_x * 2.0 + x_index) * stride_x) * scale_x
                kpt_y = ((raw_y * 2.0 + y_index) * stride_y) * scale_y
                kpt_score = float(normalize_scores(np.array([raw_score]))[0])
                keypoints.append(
                    {
                        "name": COCO_KEYPOINTS[index],
                        "x": max(0, min(frame_width - 1, int(kpt_x))),
                        "y": max(0, min(frame_height - 1, int(kpt_y))),
                        "confidence": kpt_score,
                    }
                )

            poses.append(
                {
                    "confidence": confidence,
                    "bbox": [
                        max(0, min(frame_width - 1, int(x1))),
                        max(0, min(frame_height - 1, int(y1))),
                        max(0, min(frame_width - 1, int(x2))),
                        max(0, min(frame_height - 1, int(y2))),
                    ],
                    "keypoints": keypoints,
                }
            )

        return poses

    def close(self) -> None:
        if hasattr(self, "hailo") and self.hailo is not None:
            self.hailo.close()
            self.hailo = None


def draw_pose(cv2, frame: np.ndarray, poses: List[Dict[str, Any]], keypoint_confidence: float) -> None:
    for pose in poses:
        x1, y1, x2, y2 = pose["bbox"]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (80, 220, 255), 2)
        cv2.putText(
            frame,
            f"person {pose['confidence']:.2f}",
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (80, 220, 255),
            2,
            cv2.LINE_AA,
        )

        keypoints = pose["keypoints"]
        for start, end in COCO_SKELETON:
            start_point = keypoints[start]
            end_point = keypoints[end]
            if (
                start_point["confidence"] < keypoint_confidence
                or end_point["confidence"] < keypoint_confidence
            ):
                continue
            cv2.line(
                frame,
                (start_point["x"], start_point["y"]),
                (end_point["x"], end_point["y"]),
                (0, 220, 255),
                3,
                cv2.LINE_AA,
            )

        for point in keypoints:
            if point["confidence"] < keypoint_confidence:
                continue
            center = (point["x"], point["y"])
            cv2.circle(frame, center, 5, (0, 0, 0), -1, cv2.LINE_AA)
            cv2.circle(frame, center, 3, (0, 255, 80), -1, cv2.LINE_AA)


def main() -> None:
    global np
    args = parse_args()
    cv2, hailo_class, np = load_runtime_dependencies()
    input_path = Path(args.input).expanduser()
    model_path = Path(args.model).expanduser()
    output_path = Path(args.output).expanduser() if args.output else default_output_path(input_path)

    if not input_path.exists():
        print(f"エラー: 入力動画が見つかりません: {input_path}")
        raise SystemExit(1)
    if not model_path.exists():
        print(f"エラー: HEF ファイルが見つかりません: {model_path}")
        raise SystemExit(1)

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        print(f"エラー: 入力動画を開けません: {input_path}")
        raise SystemExit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        cap.release()
        print(f"エラー: 出力動画を作成できません: {output_path}")
        raise SystemExit(1)

    runner = None
    processed = 0
    detected_frames = 0
    total_poses = 0
    start_time = time.time()

    try:
        runner = HailoPoseRunner(
            hailo_class,
            cv2,
            str(model_path),
            args.confidence,
            args.keypoint_confidence,
            args.nms_iou,
            args.max_candidates,
            args.max_poses,
        )
        print(f"入力: {input_path}")
        print(f"出力: {output_path}")
        print(f"モデル: {model_path}")
        print(f"解像度: {width}x{height}, FPS: {fps:.2f}, フレーム数: {total_frames}")
        print(f"Hailo入力: {runner.input_width}x{runner.input_height}")
        print(
            f"しきい値: confidence={args.confidence}, "
            f"keypoint={args.keypoint_confidence}, max_poses={args.max_poses}"
        )

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            poses = runner.infer(frame, dump_output=args.dump_output and processed == 0)
            if poses:
                detected_frames += 1
                total_poses += len(poses)
                draw_pose(cv2, frame, poses, args.keypoint_confidence)

            writer.write(frame)
            processed += 1

            if processed % 30 == 0:
                elapsed = time.time() - start_time
                current_fps = processed / elapsed if elapsed > 0 else 0.0
                print(
                    f"処理中: {processed}/{total_frames} フレーム, "
                    f"処理FPS {current_fps:.2f}, 検出フレーム {detected_frames}"
                )

            if args.max_frames and processed >= args.max_frames:
                break

    except KeyboardInterrupt:
        print("\n中断されました")
        raise SystemExit(130)
    finally:
        cap.release()
        writer.release()
        if runner is not None:
            runner.close()

    elapsed = time.time() - start_time
    print("完了")
    print(f"処理フレーム数: {processed}")
    print(f"処理時間: {elapsed:.1f}秒")
    print(f"処理FPS: {processed / elapsed:.2f}" if elapsed > 0 else "処理FPS: 0.00")
    print(f"POSE検出フレーム数: {detected_frames}")
    print(f"平均POSE数: {total_poses / processed:.2f}" if processed else "平均POSE数: 0.00")


if __name__ == "__main__":
    main()
