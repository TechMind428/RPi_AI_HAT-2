#!/usr/bin/env python3
"""Hailo GenAI VLM smoke test for Qwen-VL HEF files."""

import argparse
import os
import time

import cv2
import numpy as np
from hailo_platform import VDevice
from hailo_platform.genai import VLM


def load_frame(image_path, target_shape, target_dtype):
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
    parser.add_argument(
        "--model",
        required=True,
        help="Qwen-VL HEF ファイルのパス",
    )
    parser.add_argument(
        "--image",
        required=True,
        help="入力画像ファイルのパス。必要な解像度へスクリプト内でリサイズする",
    )
    parser.add_argument(
        "--prompt",
        default="この画像に写っているものを日本語で1文で説明してください。推測は書かないでください。",
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
        infer_ms = (time.perf_counter() - infer_start) * 1000

        print("\n=== 応答 ===")
        print(response)
        print("\n=== 測定 ===")
        print(f"ロード時間: {load_ms:.1f} ms")
        print(f"生成時間: {infer_ms:.1f} ms")
        print(f"応答文字数: {len(response)}")
    finally:
        if vlm is not None:
            vlm.release()
        if vdevice is not None:
            vdevice.release()


if __name__ == "__main__":
    main()
