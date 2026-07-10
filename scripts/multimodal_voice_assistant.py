#!/usr/bin/env python3
"""
Push-button multimodal voice assistant for Raspberry Pi AI HAT+ 2.

GPIOボタンを押している間だけ録音し、録音後に次の流れで応答する。
1. whisper.cpp などの外部コマンドでSTT
2. Camera Module 3 + Hailo YOLO HEFで物体検出
3. hailo-ollama のLLMへ「音声命令 + 検出結果」を渡す
4. Open JTalkなどの外部コマンドでTTS

モデルや音声コマンドはCLI引数またはJSON設定で差し替えられる。
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from hailo_ollama_client import HailoOllamaClient
except ModuleNotFoundError as exc:
    if exc.name in {"hailo_ollama_client"}:
        print(f"エラー: {exc.name}.py が見つかりません", file=sys.stderr)
        print(f"期待する場所: {SCRIPT_DIR}", file=sys.stderr)
        raise SystemExit(1)
    raise

try:
    from benchmark_utils import HailoTelemetryMonitor, SystemMonitor, flatten_hailo_telemetry
except ModuleNotFoundError:
    HailoTelemetryMonitor = None  # type: ignore[assignment]
    SystemMonitor = None  # type: ignore[assignment]
    flatten_hailo_telemetry = None  # type: ignore[assignment]


COCO80_NAMES = [
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


COCO80_JA_NAMES = {
    "person": "人",
    "bicycle": "自転車",
    "car": "車",
    "motorcycle": "オートバイ",
    "airplane": "飛行機",
    "bus": "バス",
    "train": "電車",
    "truck": "トラック",
    "boat": "ボート",
    "traffic light": "信号機",
    "fire hydrant": "消火栓",
    "stop sign": "一時停止標識",
    "parking meter": "パーキングメーター",
    "bench": "ベンチ",
    "bird": "鳥",
    "cat": "猫",
    "dog": "犬",
    "horse": "馬",
    "sheep": "羊",
    "cow": "牛",
    "elephant": "象",
    "bear": "熊",
    "zebra": "シマウマ",
    "giraffe": "キリン",
    "backpack": "リュック",
    "umbrella": "傘",
    "handbag": "ハンドバッグ",
    "tie": "ネクタイ",
    "suitcase": "スーツケース",
    "frisbee": "フリスビー",
    "skis": "スキー板",
    "snowboard": "スノーボード",
    "sports ball": "ボール",
    "kite": "凧",
    "baseball bat": "野球バット",
    "baseball glove": "野球グローブ",
    "skateboard": "スケートボード",
    "surfboard": "サーフボード",
    "tennis racket": "テニスラケット",
    "bottle": "ボトル",
    "wine glass": "ワイングラス",
    "cup": "コップ",
    "fork": "フォーク",
    "knife": "ナイフ",
    "spoon": "スプーン",
    "bowl": "ボウル",
    "banana": "バナナ",
    "apple": "りんご",
    "sandwich": "サンドイッチ",
    "orange": "オレンジ",
    "broccoli": "ブロッコリー",
    "carrot": "にんじん",
    "hot dog": "ホットドッグ",
    "pizza": "ピザ",
    "donut": "ドーナツ",
    "cake": "ケーキ",
    "chair": "椅子",
    "couch": "ソファ",
    "potted plant": "鉢植え",
    "bed": "ベッド",
    "dining table": "テーブル",
    "toilet": "トイレ",
    "tv": "テレビ",
    "laptop": "ノートPC",
    "mouse": "マウス",
    "remote": "リモコン",
    "keyboard": "キーボード",
    "cell phone": "スマートフォン",
    "microwave": "電子レンジ",
    "oven": "オーブン",
    "toaster": "トースター",
    "sink": "流し台",
    "refrigerator": "冷蔵庫",
    "book": "本",
    "clock": "時計",
    "vase": "花瓶",
    "scissors": "はさみ",
    "teddy bear": "テディベア",
    "hair drier": "ドライヤー",
    "toothbrush": "歯ブラシ",
}


DEFAULT_SYSTEM_PROMPT = """あなたはRaspberry Pi上で動くローカルAIアシスタントです。
ユーザーの音声命令と、カメラ画像から検出された物体リストをもとに、
日本語で実用的に答えてください。"""

DEFAULT_HAILO_GROUP_ID = "HAILO_OLLAMA_SHARED"

RISK_RULES = {
    "knife": {
        "type": "injury",
        "message": "ナイフは刃物なので、触れるとけがをする可能性があります。",
    },
    "scissors": {
        "type": "injury",
        "message": "はさみは刃があるため、扱い方によってけがにつながる可能性があります。",
    },
    "laptop": {
        "type": "security",
        "message": "ノートPCを夜間に放置すると、持ち去りや情報漏えいにつながる可能性があります。",
    },
    "cell phone": {
        "type": "security",
        "message": "スマートフォンを無人状態で放置すると、紛失や情報漏えいにつながる可能性があります。",
    },
    "backpack": {
        "type": "security",
        "message": "リュックには貴重品や書類が入っている可能性があるため、夜間放置は注意が必要です。",
    },
    "handbag": {
        "type": "security",
        "message": "ハンドバッグには財布や鍵が入っている可能性があるため、夜間放置は注意が必要です。",
    },
    "suitcase": {
        "type": "security",
        "message": "スーツケースを無人状態で放置すると、持ち去りや紛失につながる可能性があります。",
    },
    "banana": {
        "type": "spoilage",
        "message": "バナナは食品なので、長時間放置すると傷む可能性があります。",
    },
    "apple": {
        "type": "spoilage",
        "message": "りんごは食品なので、長時間放置すると傷む可能性があります。",
    },
    "sandwich": {
        "type": "spoilage",
        "message": "サンドイッチは食品なので、長時間放置すると腐敗や衛生問題につながる可能性があります。",
    },
    "orange": {
        "type": "spoilage",
        "message": "オレンジは食品なので、長時間放置すると傷む可能性があります。",
    },
    "broccoli": {
        "type": "spoilage",
        "message": "ブロッコリーは生鮮食品なので、長時間放置すると傷む可能性があります。",
    },
    "carrot": {
        "type": "spoilage",
        "message": "にんじんは食品なので、長時間放置すると傷む可能性があります。",
    },
    "hot dog": {
        "type": "spoilage",
        "message": "ホットドッグは調理済み食品なので、長時間放置すると衛生問題につながる可能性があります。",
    },
    "pizza": {
        "type": "spoilage",
        "message": "ピザは調理済み食品なので、長時間放置すると腐敗やにおいの原因になる可能性があります。",
    },
    "donut": {
        "type": "spoilage",
        "message": "ドーナツは食品なので、長時間放置すると品質低下や衛生問題につながる可能性があります。",
    },
    "cake": {
        "type": "spoilage",
        "message": "ケーキは食品なので、長時間放置すると腐敗や衛生問題につながる可能性があります。",
    },
}

RISK_TYPE_JA = {
    "injury": "けが",
    "security": "セキュリティ",
    "spoilage": "腐敗・衛生",
}


def ensure_hailo_group_id() -> str:
    """YOLO と hailo-ollama が同じ VDevice group を使えるようにする。"""
    group_id = os.environ.get("HAILO_VDEVICE_GROUP_ID") or os.environ.get("HAILO_OLLAMA_VDEVICE_GROUP_ID")
    if group_id:
        os.environ["HAILO_VDEVICE_GROUP_ID"] = group_id
        return group_id

    os.environ["HAILO_VDEVICE_GROUP_ID"] = DEFAULT_HAILO_GROUP_ID
    return DEFAULT_HAILO_GROUP_ID


def load_config(path: str | None) -> Dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path).expanduser()
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def config_value(args: argparse.Namespace, config: Dict[str, Any], name: str) -> Any:
    value = getattr(args, name)
    if value is not None:
        return value
    return config.get(name)


def run_command(command: str, *, timeout: int | None = None) -> subprocess.CompletedProcess:
    print(f"$ {command}")
    return subprocess.run(
        command,
        shell=True,
        check=True,
        timeout=timeout,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def is_ollama_ready(server_url: str) -> bool:
    try:
        HailoOllamaClient(server_url=server_url, timeout=2).list_models()
        return True
    except Exception:
        return False


class ManagedOllamaServer:
    def __init__(self, command: str, server_url: str, startup_timeout: int):
        self.command = command
        self.server_url = server_url
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        if is_ollama_ready(self.server_url):
            raise RuntimeError(
                "hailo-ollama はすでに起動しています。"
                " --manage-ollama を使う場合は、先に既存の hailo-ollama を停止してください。"
            )

        print(f"$ {self.command}")
        self.process = subprocess.Popen(
            self.command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )

        started_at = time.time()
        while time.time() - started_at < self.startup_timeout:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"hailo-ollama の起動に失敗しました:\n{output}")
            if is_ollama_ready(self.server_url):
                print("hailo-ollama が起動しました")
                return
            time.sleep(0.2)

        raise TimeoutError(f"hailo-ollama が {self.startup_timeout}秒以内に起動しませんでした")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            self.process = None
            return

        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=5)
        finally:
            self.process = None


class PeriodicSystemMonitor:
    """1サイクル中のPi本体側CPU、メモリ、温度を一定間隔で記録する。"""

    def __init__(self, poll_interval_sec: float):
        if SystemMonitor is None:
            raise RuntimeError("SystemMonitor を読み込めないため、Pi本体テレメトリを測定できません")
        self.monitor = SystemMonitor()
        self.poll_interval_sec = poll_interval_sec
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=max(1.0, self.poll_interval_sec + 0.5))
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.monitor.measure()
            self._stop_event.wait(self.poll_interval_sec)

    def get_statistics(self) -> Dict[str, Any]:
        return self.monitor.get_statistics()


def flatten_stats(prefix: str, stats: Dict[str, Any]) -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}
    for metric, value in stats.items():
        if isinstance(value, dict):
            for stat_name, stat_value in value.items():
                flattened[f"{prefix}_{metric}_{stat_name}"] = stat_value
        elif isinstance(value, (int, float, str)):
            flattened[f"{prefix}_{metric}"] = value
    return flattened


def write_jsonl(path: str | None, row: Dict[str, Any]) -> None:
    if not path:
        return
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_csv_row(path: str | None, row: Dict[str, Any]) -> None:
    if not path:
        return
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: List[Dict[str, Any]] = []
    fieldnames: List[str] = []
    if output_path.exists():
        with output_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            existing_rows = list(reader)

    for key in row.keys():
        if key not in fieldnames:
            fieldnames.append(key)

    with output_path.open("a", newline="", encoding="utf-8") as handle:
        if existing_rows:
            handle.close()
            with output_path.open("w", newline="", encoding="utf-8") as rewrite_handle:
                writer = csv.DictWriter(rewrite_handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(existing_rows)
                writer.writerow(row)
            return

        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if output_path.stat().st_size == 0:
            writer.writeheader()
        writer.writerow(row)


class PushButton:
    def __init__(self, gpio: int, keyboard: bool = False, trigger_only: bool = False):
        self.keyboard = keyboard
        self.trigger_only = trigger_only
        self.button = None
        if keyboard:
            return

        try:
            from gpiozero import Button
        except ImportError as exc:  # pragma: no cover - Raspberry Pi実機用
            raise RuntimeError(
                "gpiozero を読み込めません。GPIOを使う場合は python3-gpiozero を導入してください。"
                " 動作確認だけなら --keyboard を使えます。"
            ) from exc

        self.button = Button(gpio, pull_up=True, bounce_time=0.05)

    def wait_for_press(self) -> None:
        if self.keyboard:
            if self.trigger_only:
                input("Enterを押すと机に置いてあるものを認識します...")
            else:
                input("Enterを押すと録音を開始します...")
            return
        if self.trigger_only:
            print("ボタンを押すと机に置いてあるものを認識します...")
        else:
            print("ボタンを押すと録音を開始します...")
        self.button.wait_for_press()

    def wait_for_release(self) -> None:
        if self.keyboard:
            if self.trigger_only:
                input("もう一度Enterを押すと実行します...")
            else:
                input("もう一度Enterを押すと録音を停止します...")
            return
        if self.trigger_only:
            print("ボタンを離すと実行します...")
        else:
            print("ボタンを離すと録音を停止します...")
        self.button.wait_for_release()


class AudioRecorder:
    def __init__(self, command_template: str):
        self.command_template = command_template
        self.process: subprocess.Popen | None = None

    def start(self, audio_path: Path) -> None:
        command = self.command_template.format(audio=shlex.quote(str(audio_path)))
        print(f"$ {command}")
        self.process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )

    def stop(self) -> None:
        if self.process is None:
            return

        try:
            # arecord は Ctrl-C 相当の SIGINT で止めると WAV ヘッダを閉じやすい。
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=3)
        finally:
            self.process = None


class VisionDetector:
    def __init__(self, model_path: str, resolution: tuple[int, int], confidence: float):
        try:
            from picamera2 import Picamera2
            from hailo_platform_detection import HailoYoloRunner
        except ImportError as exc:  # pragma: no cover - Raspberry Pi実機用
            raise RuntimeError(
                "Picamera2 / Hailo YOLO 実行環境を読み込めません。"
                " Raspberry Pi 実機で第3章のセットアップが完了しているか確認してください。"
            ) from exc

        self.resolution = resolution
        self.runner = HailoYoloRunner(model_path, confidence=confidence)
        self.camera = Picamera2()
        config = self.camera.create_preview_configuration(
            main={"size": resolution, "format": "RGB888"}
        )
        self.camera.configure(config)
        self.camera.start()
        time.sleep(0.5)

    def detect(self) -> List[Dict[str, Any]]:
        frame = self.camera.capture_array()
        detections = self.runner.infer(frame)
        for detection in detections:
            class_id = int(detection.get("class_id", -1))
            detection["label"] = (
                COCO80_NAMES[class_id]
                if 0 <= class_id < len(COCO80_NAMES)
                else f"class_{class_id}"
            )
        return detections

    def close(self) -> None:
        try:
            self.camera.stop()
        except Exception:
            pass
        self.runner.close()


def summarize_detections(detections: List[Dict[str, Any]], max_items: int = 12) -> str:
    if not detections:
        return "検出なし"

    counts = Counter(str(detection.get("label", "unknown")) for detection in detections)
    parts = [
        f"{COCO80_JA_NAMES.get(label, label)} x{count}"
        for label, count in counts.most_common(max_items)
    ]
    return ", ".join(parts)


def summarize_detections_for_sentence(detections: List[Dict[str, Any]], max_items: int = 12) -> str:
    if not detections:
        return ""

    counts = Counter(str(detection.get("label", "unknown")) for detection in detections)
    parts = []
    for label, count in counts.most_common(max_items):
        ja_label = COCO80_JA_NAMES.get(label, label)
        if count == 1:
            parts.append(ja_label)
        else:
            parts.append(f"{ja_label}が{count}個")
    return "、".join(parts)


def format_detections_for_prompt(detections: List[Dict[str, Any]], max_items: int = 12) -> str:
    if not detections:
        return "なし"

    counts = Counter(str(detection.get("label", "unknown")) for detection in detections)
    parts = []
    for label, count in counts.most_common(max_items):
        ja_label = COCO80_JA_NAMES.get(label, label)
        if count == 1:
            parts.append(ja_label)
        else:
            parts.append(f"{ja_label}が{count}個")
    return "、".join(parts)


def analyze_risks(detections: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    risks: List[Dict[str, str]] = []
    seen_labels: set[str] = set()
    for detection in detections:
        label = str(detection.get("label", "unknown"))
        if label in seen_labels or label not in RISK_RULES:
            continue
        seen_labels.add(label)
        rule = RISK_RULES[label]
        risk_type = str(rule["type"])
        risks.append(
            {
                "label": label,
                "label_ja": COCO80_JA_NAMES.get(label, label),
                "type": risk_type,
                "type_ja": RISK_TYPE_JA.get(risk_type, risk_type),
                "message": str(rule["message"]),
            }
        )
    return risks


def summarize_risks(risks: List[Dict[str, str]]) -> str:
    if not risks:
        return "該当なし"
    return "、".join(f"{risk['label_ja']}({risk['type_ja']})" for risk in risks)


def format_risks_for_prompt(risks: List[Dict[str, str]]) -> str:
    if not risks:
        return "ルール上、注意対象は見つかりませんでした。"
    return " ".join(
        f"{risk['label_ja']}: {risk['message']}"
        for risk in risks
    )


def format_allowed_risk_labels(risks: List[Dict[str, str]]) -> str:
    if not risks:
        return "なし"
    return "、".join(risk["label_ja"] for risk in risks)


def compact_text(value: str) -> str:
    return " ".join(value.split())


def build_prompt(
    command_text: str,
    detections_block: str,
    risk_block: str,
    allowed_risk_labels: str,
    system_prompt: str,
) -> str:
    prompt = (
        f"机に置かれている物の検出結果が、{detections_block}でした。 "
        f"アプリ側のルール判定では、{risk_block} "
        f"{compact_text(command_text)} "
        f"回答に使ってよい注意対象の物名は「{allowed_risk_labels}」だけです。 "
        "ルール判定にない物名は回答に含めないでください。 "
        "注意対象の物名と理由だけを、日本語で1文で答えてください。"
    )
    return compact_text(prompt)


def build_template_answer(detections_sentence: str, risks: List[Dict[str, str]]) -> str:
    if risks:
        return " ".join(risk["message"] for risk in risks)
    if detections_sentence:
        return f"机の上には、{detections_sentence}があります。ルール上、注意対象は見つかりませんでした。"
    return "物体は検出できませんでした。"


def run_stt(command_template: str, audio_path: Path, text_path: Path, timeout: int) -> str:
    text_base = text_path.with_suffix("")
    command = command_template.format(
        audio=shlex.quote(str(audio_path)),
        text=shlex.quote(str(text_path)),
        text_base=shlex.quote(str(text_base)),
    )
    completed = run_command(command, timeout=timeout)
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip())

    if not text_path.exists():
        raise FileNotFoundError(
            f"STT結果ファイルが見つかりません: {text_path}. "
            "--stt-command の {text} または {text_base} 指定を確認してください。"
        )

    return text_path.read_text(encoding="utf-8").strip()


def read_wav_as_float32_mono(audio_path: Path) -> Any:
    """16kHz/mono/S16_LE のWAVをHailo Speech2Text用のfloat32配列に変換する"""
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Raspberry Pi実機用
        raise RuntimeError("Hailo STTには numpy が必要です") from exc

    with wave.open(str(audio_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    if channels != 1 or sample_width != 2 or frame_rate != 16000:
        raise ValueError(
            "Hailo STTには 16kHz / mono / S16_LE のWAVが必要です。"
            f" 実際: channels={channels}, sample_width={sample_width}, frame_rate={frame_rate}"
        )

    audio_i16 = np.frombuffer(frames, dtype="<i2")
    return audio_i16.astype("<f4") / 32768.0


def clean_stt_text(text: str) -> str:
    """短い音声認識結果に混じる末尾ノイズを軽く整える"""
    text = text.strip()
    text = re.sub(r"[ \t\r\n]+", " ", text)
    text = re.sub(r"([。．.!?？！])\s*[A-Za-z]+$", r"\1", text)
    text = re.sub(r"([。．.!?？！])\s*[音Pp]+$", r"\1", text)
    return text.strip()


def run_hailo_stt(model_path: str, audio_path: Path) -> str:
    """Hailo GenAI Speech2Text APIで音声認識を行う"""
    try:
        from hailo_platform import VDevice
        from hailo_platform.genai import Speech2Text, Speech2TextTask
    except ImportError as exc:  # pragma: no cover - Raspberry Pi実機用
        raise RuntimeError(
            "Hailo Speech2Text APIを読み込めません。"
            " HailoRT 5.3.0 の Python wheel が仮想環境に入っているか確認してください。"
        ) from exc

    audio = read_wav_as_float32_mono(audio_path)
    model = str(Path(model_path).expanduser())
    if not Path(model).exists():
        raise FileNotFoundError(f"Hailo Whisper HEF が見つかりません: {model}")

    params = VDevice.create_params()
    group_id = os.environ.get("HAILO_VDEVICE_GROUP_ID") or os.environ.get("HAILO_OLLAMA_VDEVICE_GROUP_ID")
    if group_id and hasattr(params, "group_id"):
        params.group_id = group_id
        print(f"Hailo STT VDevice group_id: {group_id}")

    print(f"Hailo STTモデル: {model}")
    print(f"Hailo STT音声サンプル数: {len(audio)}")
    vdevice = VDevice(params)
    try:
        started_at = time.perf_counter()
        stt = Speech2Text(vdevice, model)
        load_ms = (time.perf_counter() - started_at) * 1000

        infer_started_at = time.perf_counter()
        text = stt.generate_all_text(
            audio,
            task=Speech2TextTask.TRANSCRIBE,
            language="ja",
        )
        infer_ms = (time.perf_counter() - infer_started_at) * 1000

        print(f"Hailo STT測定: load={load_ms:.1f}ms, infer={infer_ms:.1f}ms")
        stt.release()
        return clean_stt_text(str(text))
    finally:
        vdevice.release()


def run_tts(command_template: str, play_command_template: str, response_text: str, wav_path: Path, timeout: int) -> None:
    text_path = wav_path.with_suffix(".txt")
    tts_text = prepare_tts_text(response_text)
    print(f"読み上げテキスト: {tts_text}")
    text_path.write_text(tts_text, encoding="utf-8")

    if command_template:
        command = command_template.format(
            text=shlex.quote(str(text_path)),
            wav=shlex.quote(str(wav_path)),
        )
        completed = run_command(command, timeout=timeout)
        if completed.stderr.strip():
            print(completed.stderr.strip())

    if play_command_template:
        command = play_command_template.format(wav=shlex.quote(str(wav_path)))
        completed = run_command(command, timeout=timeout)
        if completed.stderr.strip():
            print(completed.stderr.strip())


def prepare_tts_text(text: str) -> str:
    """LLMのMarkdown風出力をOpen JTalkで読みやすい文章に整える。"""
    text = text.strip()
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^[ \t]*[-*・]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*\d+[.)、]\s*", "", text, flags=re.MULTILINE)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]*\n+[ \t]*", "。", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.replace("：", "、").replace(":", "、")
    text = re.sub(r"。\s*。+", "。", text)
    return text.strip(" 。\n\t") + "。"


def warmup_llm(
    client: HailoOllamaClient,
    model_name: str,
    prompt: str,
    temperature: float,
    llm_api: str,
) -> None:
    print("LLMウォームアップを実行します")
    if llm_api == "generate":
        response = client.generate(
            model_name=model_name,
            prompt=prompt,
            max_tokens=16,
            temperature=temperature,
        )
    else:
        response = client.chat(
            model_name=model_name,
            prompt=prompt,
            max_tokens=16,
            temperature=temperature,
        )
    print(f"ウォームアップ応答: {response.response_text.strip()}")
    print(
        f"ウォームアップ測定: TTFT={response.ttft_ms:.1f}ms, "
        f"total={response.total_ms:.1f}ms"
    )


def chat_with_retry(
    client: HailoOllamaClient,
    *,
    model_name: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    llm_api: str,
    retries: int,
    retry_wait: float,
):
    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            if llm_api == "generate":
                return client.generate(
                    model_name=model_name,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            return client.chat(
                model_name=model_name,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except RuntimeError as exc:
            last_error = exc
            if attempt > retries:
                break
            print(f"LLM呼び出しを再試行します ({attempt}/{retries}): {exc}")
            time.sleep(retry_wait)
    raise last_error if last_error is not None else RuntimeError("LLM呼び出しに失敗しました")


def parse_resolution(value: str) -> tuple[int, int]:
    width, height = value.lower().split("x", 1)
    return int(width), int(height)


def run_vision_detection(
    model_path: str,
    resolution: tuple[int, int],
    confidence: float,
    release_wait: float,
) -> List[Dict[str, Any]]:
    detector = VisionDetector(model_path, resolution, confidence)
    try:
        return detector.detect()
    finally:
        # hailo-ollama と同じHATを使うため、LLMへ渡す前にYOLO側を解放する。
        detector.close()
        del detector
        gc.collect()
        if release_wait > 0:
            print(f"Hailoデバイス解放待ち: {release_wait:.1f}秒")
            time.sleep(release_wait)


def write_vision_detection_json(
    output_path: Path,
    model_path: str,
    resolution: tuple[int, int],
    confidence: float,
) -> None:
    detections = run_vision_detection(
        model_path=model_path,
        resolution=resolution,
        confidence=confidence,
        release_wait=0,
    )
    output_path.write_text(json.dumps(detections, ensure_ascii=False), encoding="utf-8")


def run_vision_detection_subprocess(
    model_path: str,
    resolution: tuple[int, int],
    confidence: float,
    release_wait: float,
    work_dir: Path,
) -> List[Dict[str, Any]]:
    output_path = work_dir / f"assistant_detections_{time.strftime('%Y%m%d_%H%M%S')}.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--detect-once",
        "--vision-output",
        str(output_path),
        "--vision-model",
        model_path,
        "--resolution",
        f"{resolution[0]}x{resolution[1]}",
        "--confidence",
        str(confidence),
    ]
    print("$ " + " ".join(shlex.quote(part) for part in command))
    subprocess.run(command, check=True)
    if release_wait > 0:
        print(f"Hailoデバイス解放待ち: {release_wait:.1f}秒")
        time.sleep(release_wait)
    return json.loads(output_path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="GPIOボタン式マルチモーダル音声アシスタント")
    parser.add_argument("--config", help="JSON設定ファイル")
    parser.add_argument("--gpio", type=int, default=None, help="録音ボタンに使うGPIO番号")
    parser.add_argument("--keyboard", action="store_true", help="GPIOの代わりにEnterキーで操作する")
    parser.add_argument("--vision-model", default=None, help="YOLO HEFファイル")
    parser.add_argument("--llm-model", default=None, help="hailo-ollama のモデル名")
    parser.add_argument(
        "--llm-api",
        choices=("generate", "chat"),
        default=None,
        help="hailo-ollama の呼び出しAPI。既定値は generate",
    )
    parser.add_argument(
        "--answer-mode",
        choices=("template", "llm"),
        default=None,
        help="回答文の作り方。llm はLLMで回答し、template は検出結果から固定文を作る",
    )
    parser.add_argument("--server", default=None, help="hailo-ollama のURL")
    parser.add_argument("--resolution", default=None, help="カメラ入力解像度。例: 640x640")
    parser.add_argument("--confidence", type=float, default=None, help="YOLO検出の信頼度しきい値")
    parser.add_argument("--max-tokens", type=int, default=None, help="LLMの最大出力トークン数")
    parser.add_argument("--temperature", type=float, default=None, help="LLMのtemperature")
    parser.add_argument("--record-command", default=None, help="録音コマンド。{audio} を使える")
    parser.add_argument(
        "--stt-engine",
        choices=("command", "hailo"),
        default=None,
        help="STTの実行方式。command は外部コマンド、hailo はHailo Whisper HEFを使う",
    )
    parser.add_argument("--stt-command", default=None, help="STTコマンド。{audio}, {text}, {text_base} を使える")
    parser.add_argument("--hailo-stt-model", default=None, help="Hailo Whisper HEFファイル")
    parser.add_argument(
        "--trigger-only",
        action="store_true",
        help="ボタンを録音ではなく実行トリガーとして使い、固定質問で画像認識とLLM応答を行う",
    )
    parser.add_argument(
        "--fixed-command",
        default=None,
        help="--trigger-only で使う固定質問",
    )
    parser.add_argument("--tts-command", default=None, help="TTSコマンド。{text}, {wav} を使える")
    parser.add_argument("--play-command", default=None, help="音声再生コマンド。{wav} を使える")
    parser.add_argument("--system-prompt", default=None, help="LLMへ渡すシステム指示")
    parser.add_argument("--show-prompt", action="store_true", help="LLMへ渡すプロンプトを表示する")
    parser.add_argument("--warmup-prompt", default=None, help="起動時にLLMへ投げる短いウォームアッププロンプト")
    parser.add_argument("--no-warmup", action="store_true", help="起動時のLLMウォームアップを行わない")
    parser.add_argument("--work-dir", default=None, help="音声一時ファイルの保存先")
    parser.add_argument("--stt-timeout", type=int, default=None, help="STTコマンドのタイムアウト秒")
    parser.add_argument("--tts-timeout", type=int, default=None, help="TTS/再生コマンドのタイムアウト秒")
    parser.add_argument("--release-wait", type=float, default=None, help="YOLO解放後、LLM呼び出し前に待つ秒数")
    parser.add_argument("--manage-ollama", action="store_true", help="YOLO後にhailo-ollamaを起動し、LLM応答後に停止する")
    parser.add_argument("--ollama-command", default=None, help="--manage-ollamaで使うhailo-ollama起動コマンド")
    parser.add_argument("--ollama-startup-timeout", type=int, default=None, help="hailo-ollama起動待ちの最大秒数")
    parser.add_argument("--llm-retries", type=int, default=None, help="LLM呼び出し失敗時の再試行回数")
    parser.add_argument("--llm-retry-wait", type=float, default=None, help="LLM呼び出し再試行前に待つ秒数")
    parser.add_argument("--telemetry", action="store_true", help="Pi本体とHAT側のテレメトリを1サイクルごとに記録する")
    parser.add_argument("--telemetry-output", default=None, help="テレメトリCSVの保存先")
    parser.add_argument("--telemetry-jsonl", default=None, help="テレメトリ詳細JSONLの保存先")
    parser.add_argument(
        "--telemetry-poll-interval",
        type=float,
        default=None,
        help="Pi本体とHAT側テレメトリを読む間隔（秒）",
    )
    parser.add_argument(
        "--hat-telemetry-sampling-ms",
        type=int,
        default=None,
        help="HAT側NNC使用率などを取得するときのサンプリング時間（ミリ秒）",
    )
    parser.add_argument("--detect-once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--vision-output", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--once", action="store_true", help="1回だけ実行して終了する")
    args = parser.parse_args()

    config = load_config(args.config)

    gpio = int(config_value(args, config, "gpio") or 26)
    vision_model = config_value(args, config, "vision_model") or "/usr/share/hailo-models/yolov8m_h10.hef"
    llm_model = config_value(args, config, "llm_model") or "qwen3:1.7b"
    llm_api = config_value(args, config, "llm_api") or "generate"
    answer_mode = config_value(args, config, "answer_mode") or "llm"
    server = config_value(args, config, "server") or "http://localhost:8000"
    resolution = parse_resolution(config_value(args, config, "resolution") or "640x640")
    confidence = float(config_value(args, config, "confidence") or 0.5)
    max_tokens = int(config_value(args, config, "max_tokens") or 96)
    temperature = float(config_value(args, config, "temperature") or 0.0)
    record_command = (
        config_value(args, config, "record_command")
        or "arecord -f S16_LE -r 16000 -c 1 {audio}"
    )
    stt_engine = config_value(args, config, "stt_engine") or "command"
    hailo_stt_model = (
        config_value(args, config, "hailo_stt_model")
        or "/home/admin/hailo-stt-models/Whisper-Base.hef"
    )
    trigger_only = args.trigger_only or bool(config.get("trigger_only", False))
    fixed_command = (
        config_value(args, config, "fixed_command")
        or "机の上に注意が必要なものはありますか。"
    )
    stt_command = (
        config_value(args, config, "stt_command")
        or "whisper-cli -m ~/whisper.cpp/models/ggml-base.bin -f {audio} -l ja -otxt -of {text_base}"
    )
    tts_command = (
        config_value(args, config, "tts_command")
        or "open_jtalk -x /var/lib/mecab/dic/open-jtalk/naist-jdic "
        "-m /usr/share/hts-voice/mei/mei_normal.htsvoice -ow {wav} {text}"
    )
    play_command = config_value(args, config, "play_command") or "aplay {wav}"
    system_prompt = config_value(args, config, "system_prompt") or DEFAULT_SYSTEM_PROMPT
    show_prompt = args.show_prompt or bool(config.get("show_prompt", False))
    warmup_prompt = (
        config_value(args, config, "warmup_prompt")
        or "準備確認です。短く「準備完了」と答えてください。"
    )
    no_warmup = args.no_warmup or bool(config.get("no_warmup", False))
    work_dir = Path(config_value(args, config, "work_dir") or tempfile.gettempdir()).expanduser()
    stt_timeout = int(config_value(args, config, "stt_timeout") or 120)
    tts_timeout = int(config_value(args, config, "tts_timeout") or 60)
    release_wait_value = config_value(args, config, "release_wait")
    release_wait = 2.0 if release_wait_value is None else float(release_wait_value)
    manage_ollama = args.manage_ollama or bool(config.get("manage_ollama", False))
    ollama_command = config_value(args, config, "ollama_command") or "hailo-ollama"
    ollama_startup_timeout = int(config_value(args, config, "ollama_startup_timeout") or 30)
    llm_retries = int(config_value(args, config, "llm_retries") or 3)
    llm_retry_wait = float(config_value(args, config, "llm_retry_wait") or 2.0)
    telemetry_enabled = args.telemetry or bool(config.get("telemetry", False))
    telemetry_output = config_value(args, config, "telemetry_output")
    telemetry_jsonl = config_value(args, config, "telemetry_jsonl")
    telemetry_poll_interval = float(config_value(args, config, "telemetry_poll_interval") or 0.5)
    hat_telemetry_sampling_ms = int(config_value(args, config, "hat_telemetry_sampling_ms") or 100)
    if telemetry_enabled and (
        SystemMonitor is None
        or HailoTelemetryMonitor is None
        or flatten_hailo_telemetry is None
    ):
        raise RuntimeError(
            "テレメトリ測定には benchmark_utils.py と psutil が必要です。"
            " 第3章の仮想環境と scripts 一式を確認してください。"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    hailo_group_id = ensure_hailo_group_id()

    if args.detect_once:
        if not args.vision_output:
            raise SystemExit("--detect-once には --vision-output が必要です")
        write_vision_detection_json(
            output_path=Path(args.vision_output).expanduser(),
            model_path=vision_model,
            resolution=resolution,
            confidence=confidence,
        )
        return

    print("=== マルチモーダル音声アシスタント ===")
    print(f"GPIO: {gpio} ({'keyboard' if args.keyboard else 'button'})")
    print(f"YOLO: {vision_model}")
    print(f"LLM: {llm_model}")
    print(f"LLM API: /api/{llm_api}")
    print(f"回答生成: {answer_mode}")
    print(f"hailo-ollama: {server}")
    print(f"Hailo VDevice group_id: {hailo_group_id}")
    print(f"解像度: {resolution[0]}x{resolution[1]}")
    print(f"入力モード: {'trigger-only' if trigger_only else 'voice'}")
    if telemetry_enabled:
        print("テレメトリ: 有効")
        if telemetry_output:
            print(f"テレメトリCSV: {Path(telemetry_output).expanduser()}")
        if telemetry_jsonl:
            print(f"テレメトリJSONL: {Path(telemetry_jsonl).expanduser()}")
    if not trigger_only:
        print(f"STT: {stt_engine}")
        if stt_engine == "hailo":
            print(f"Hailo STT: {hailo_stt_model}")

    button = PushButton(gpio=gpio, keyboard=args.keyboard, trigger_only=trigger_only)
    recorder = AudioRecorder(record_command)
    llm = HailoOllamaClient(server_url=server, timeout=240)

    try:
        if manage_ollama:
            print("hailo-ollama はYOLO推論後にアプリ側で起動します")
        elif not no_warmup:
            warmup_llm(llm, llm_model, warmup_prompt, temperature, llm_api)

        cycle_index = 0
        while True:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            audio_path = work_dir / f"assistant_input_{timestamp}.wav"
            text_path = work_dir / f"assistant_input_{timestamp}.txt"
            tts_wav_path = work_dir / f"assistant_response_{timestamp}.wav"
            cycle_index += 1
            cycle_started_at = 0.0
            system_cycle_monitor: PeriodicSystemMonitor | None = None
            hailo_cycle_monitor: HailoTelemetryMonitor | None = None

            if trigger_only:
                button.wait_for_press()
                print("注意対象チェックを開始します")
                button.wait_for_release()
                command_text = fixed_command
                print(f"固定質問: {command_text}")
                cycle_started_at = time.perf_counter()
                if telemetry_enabled:
                    system_cycle_monitor = PeriodicSystemMonitor(telemetry_poll_interval)
                    hailo_cycle_monitor = HailoTelemetryMonitor(
                        enabled=True,
                        sampling_period_ms=hat_telemetry_sampling_ms,
                        poll_interval_sec=telemetry_poll_interval,
                    )
                    system_cycle_monitor.start()
                    hailo_cycle_monitor.start()
            else:
                button.wait_for_press()
                print("録音開始")
                recorder.start(audio_path)
                button.wait_for_release()
                recorder.stop()
                print(f"録音終了: {audio_path}")

                cycle_started_at = time.perf_counter()
                if telemetry_enabled:
                    system_cycle_monitor = PeriodicSystemMonitor(telemetry_poll_interval)
                    hailo_cycle_monitor = HailoTelemetryMonitor(
                        enabled=True,
                        sampling_period_ms=hat_telemetry_sampling_ms,
                        poll_interval_sec=telemetry_poll_interval,
                    )
                    system_cycle_monitor.start()
                    hailo_cycle_monitor.start()
                print("STTを実行します")
                if stt_engine == "hailo":
                    command_text = run_hailo_stt(hailo_stt_model, audio_path)
                    text_path.write_text(command_text, encoding="utf-8")
                else:
                    command_text = run_stt(stt_command, audio_path, text_path, stt_timeout)
                print(f"音声認識結果: {command_text}")

            print("画像推論を実行します")
            detections = run_vision_detection_subprocess(
                vision_model,
                resolution,
                confidence,
                release_wait,
                work_dir,
            )
            detections_summary = summarize_detections(detections)
            detections_sentence = summarize_detections_for_sentence(detections)
            detections_block = format_detections_for_prompt(detections)
            risks = analyze_risks(detections)
            risk_summary = summarize_risks(risks)
            risk_block = format_risks_for_prompt(risks)
            allowed_risk_labels = format_allowed_risk_labels(risks)
            print(f"検出結果: {detections_summary}")
            print(f"ルール判定: {risk_summary}")

            llm_ttft_ms = None
            llm_total_ms = None
            llm_tokens_per_sec = None
            llm_eval_count = None
            llm_done_reason = ""
            if answer_mode == "template":
                response_text = build_template_answer(detections_sentence, risks)
                print("テンプレートで回答を生成します")
                print(f"回答: {response_text}")
            else:
                prompt = build_prompt(
                    command_text,
                    detections_block,
                    risk_block,
                    allowed_risk_labels,
                    system_prompt,
                )
                if show_prompt:
                    print("=== LLMへ渡すプロンプト ===")
                    print(prompt)
                    print("=== プロンプトここまで ===")
                print("LLMで回答を生成します")
                managed_server = None
                try:
                    if manage_ollama:
                        managed_server = ManagedOllamaServer(
                            command=ollama_command,
                            server_url=server,
                            startup_timeout=ollama_startup_timeout,
                        )
                        managed_server.start()
                        print(f"LLMモデルを確認します: {llm_model}")
                        llm.pull_model(llm_model)

                    response = chat_with_retry(
                        llm,
                        model_name=llm_model,
                        prompt=prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        llm_api=llm_api,
                        retries=llm_retries,
                        retry_wait=llm_retry_wait,
                    )
                finally:
                    if managed_server is not None:
                        managed_server.stop()
                response_text = response.response_text.strip()
                llm_ttft_ms = response.ttft_ms
                llm_total_ms = response.total_ms
                llm_tokens_per_sec = response.tokens_per_sec
                llm_eval_count = response.eval_count
                llm_done_reason = response.done_reason
                print(f"回答: {response_text}")
                print(
                    f"LLM測定: TTFT={response.ttft_ms:.1f}ms, "
                    f"total={response.total_ms:.1f}ms, TPS={response.tokens_per_sec:.2f}"
                )

            print("TTSで読み上げます")
            run_tts(tts_command, play_command, response_text, tts_wav_path, tts_timeout)

            if telemetry_enabled:
                if system_cycle_monitor is not None:
                    system_cycle_monitor.stop()
                if hailo_cycle_monitor is not None:
                    hailo_cycle_monitor.stop()

                cycle_total_ms = (time.perf_counter() - cycle_started_at) * 1000
                system_stats = system_cycle_monitor.get_statistics() if system_cycle_monitor else {}
                hailo_stats = hailo_cycle_monitor.get_statistics() if hailo_cycle_monitor else {}
                hailo_measurements = hailo_cycle_monitor.snapshot() if hailo_cycle_monitor else []
                if hailo_cycle_monitor is not None:
                    hailo_cycle_monitor.close()

                telemetry_row: Dict[str, Any] = {
                    "timestamp": timestamp,
                    "cycle_index": cycle_index,
                    "input_mode": "trigger-only" if trigger_only else "voice",
                    "stt_engine": "" if trigger_only else stt_engine,
                    "answer_mode": answer_mode,
                    "llm_model": llm_model,
                    "llm_api": llm_api,
                    "vision_model": vision_model,
                    "resolution": f"{resolution[0]}x{resolution[1]}",
                    "confidence": confidence,
                    "cycle_total_ms": round(cycle_total_ms, 2),
                    "detections_count": len(detections),
                    "detections_summary": detections_summary,
                    "risk_count": len(risks),
                    "risk_summary": risk_summary,
                    "command_text": command_text,
                    "response_chars": len(response_text),
                    "response_text": response_text,
                    "llm_ttft_ms": round(llm_ttft_ms, 2) if llm_ttft_ms is not None else None,
                    "llm_total_ms": round(llm_total_ms, 2) if llm_total_ms is not None else None,
                    "llm_tokens_per_sec": round(llm_tokens_per_sec, 3) if llm_tokens_per_sec is not None else None,
                    "llm_eval_count": llm_eval_count,
                    "llm_done_reason": llm_done_reason,
                }
                telemetry_row.update(flatten_stats("system", system_stats))
                telemetry_row.update(flatten_hailo_telemetry(hailo_stats))
                append_csv_row(telemetry_output, telemetry_row)
                write_jsonl(
                    telemetry_jsonl,
                    {
                        "summary": telemetry_row,
                        "system_stats": system_stats,
                        "hailo_stats": hailo_stats,
                        "hailo_measurements": hailo_measurements,
                    },
                )
                print(
                    "テレメトリ: "
                    f"cycle={cycle_total_ms:.1f}ms, "
                    f"system_samples={system_stats.get('measurement_count', 0)}, "
                    f"hailo_samples={hailo_stats.get('measurement_count', 0)}"
                )

            if args.once:
                break
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
