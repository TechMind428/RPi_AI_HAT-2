#!/usr/bin/env python3
"""Hailo GenAI VLM smoke test for Qwen-VL HEF files."""

import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from hailo_platform import VDevice
from hailo_platform.genai import VLM


SPECIAL_TOKENS = ("<|im_end|>", "<|endoftext|>")


def clean_response(text: str) -> str:
    cleaned = text
    for token in SPECIAL_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned.strip()


def append_csv_row(path: str, row: dict):
    csv_path = Path(path).expanduser()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl(path: str, row: dict):
    jsonl_path = Path(path).expanduser()
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_frame(image_path: str, target_shape, target_dtype):
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"画像を読み込めません: {image_path}")

    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    height, width, channels = target_shape
    if image.shape[:2] != (height, width):
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)

    if channels == 1 and image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        image = np.expand_dims(image, axis=2)

    return image.astype(target_dtype)


def main():
    parser = argparse.ArgumentParser(
        description="Hailo GenAI VLM の最小動作確認を行う"
    )
    parser.add_argument("--model", required=True, help="Qwen-VL HEF ファイルのパス")
    parser.add_argument(
        "--image",
        required=True,
        help="入力画像ファイルのパス。必要な解像度へスクリプト内でリサイズする",
    )
    parser.add_argument(
        "--prompt",
        default="Describe this image in one short English sentence.",
        help="VLM に渡す質問文",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=64,
        help="生成する最大トークン数",
    )
    parser.add_argument(
        "--no-optimize-memory",
        action="store_true",
        help="デバイス側メモリ最適化を無効化する",
    )
    parser.add_argument("--output-csv", default=None, help="測定結果CSVの保存先")
    parser.add_argument("--output-jsonl", default=None, help="測定結果JSONLの保存先")
    parser.add_argument("--run-id", default="", help="測定回の識別子。例: run1")
    args = parser.parse_args()

    if not os.path.isfile(args.model):
        raise FileNotFoundError(args.model)
    if not os.path.isfile(args.image):
        raise FileNotFoundError(args.image)

    optimize_memory = not args.no_optimize_memory

    print(f"モデル: {args.model}")
    print(f"画像: {args.image}")
    print(f"メモリ最適化: {optimize_memory}")

    vdevice = None
    vlm = None
    try:
        load_start = time.perf_counter()
        vdevice = VDevice()
        vlm = VLM(vdevice, args.model, optimize_memory)
        load_ms = (time.perf_counter() - load_start) * 1000

        frame_shape = vlm.input_frame_shape()
        frame_dtype = vlm.input_frame_format_type()
        frame_size = vlm.input_frame_size()
        frame_order = vlm.input_frame_format_order()

        print("入力フレーム条件:")
        print(f"  shape: {frame_shape}")
        print(f"  dtype: {frame_dtype}")
        print(f"  size: {frame_size} bytes")
        print(f"  order: {frame_order}")

        frame = load_frame(args.image, frame_shape, frame_dtype)
        print(f"変換後画像: shape={frame.shape}, dtype={frame.dtype}, bytes={frame.nbytes}")

        prompt = [
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": args.prompt},
                ],
            }
        ]

        vlm.clear_context()
        infer_start = time.perf_counter()
        response = vlm.generate_all(
            prompt=prompt,
            frames=[frame],
            max_generated_tokens=args.max_tokens,
        )
        response = clean_response(response)
        infer_ms = (time.perf_counter() - infer_start) * 1000

        print("\n=== 応答 ===")
        print(response)
        print("\n=== 測定 ===")
        print(f"ロード時間: {load_ms:.1f} ms")
        print(f"生成時間: {infer_ms:.1f} ms")
        print(f"応答文字数: {len(response)}")

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "run_id": args.run_id,
            "model_path": str(Path(args.model).expanduser()),
            "model_file": Path(args.model).name,
            "image_path": str(Path(args.image).expanduser()),
            "prompt": args.prompt,
            "max_tokens": args.max_tokens,
            "optimize_memory": optimize_memory,
            "input_shape": "x".join(str(x) for x in frame_shape),
            "input_dtype": str(frame_dtype),
            "input_order": str(frame_order),
            "load_ms": round(load_ms, 3),
            "generate_ms": round(infer_ms, 3),
            "response_chars": len(response),
            "response": response,
        }
        if args.output_csv:
            append_csv_row(args.output_csv, row)
            print(f"CSVを保存しました: {Path(args.output_csv).expanduser()}")
        if args.output_jsonl:
            append_jsonl(args.output_jsonl, row)
            print(f"JSONLを保存しました: {Path(args.output_jsonl).expanduser()}")
    finally:
        if vlm is not None:
            vlm.release()
        if vdevice is not None:
            vdevice.release()


if __name__ == "__main__":
    main()
