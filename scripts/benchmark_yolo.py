#!/usr/bin/env python3
"""
YOLO Object Detection Benchmark - 物体検出性能測定スクリプト

このスクリプトは、Hailo NPU を使用した YOLO 系 HEF モデルの性能を測定します：
- FPS（Frames Per Second）
- レイテンシ（処理時間）
- 検出精度
- システムリソース使用率
- 長時間稼働の安定性
"""

import argparse
import time
import sys
import threading
from pathlib import Path
import numpy as np
import psutil

# 共通ユーティリティをインポート
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from benchmark_utils import SystemMonitor, DataSaver, FPSCounter, PerformanceMetrics
    from hailo_platform_detection import HailoYoloRunner
except ModuleNotFoundError as exc:
    if exc.name in {"benchmark_utils", "hailo_platform_detection"}:
        print(f"エラー: {exc.name}.py が見つかりません")
        print(f"期待する場所: {SCRIPT_DIR}")
        print("scripts ディレクトリ一式を同じ場所に置いたまま実行してください")
        raise SystemExit(1)
    raise

try:
    from picamera2 import Picamera2
    import cv2
except ImportError as e:
    print(f"警告: 必要なライブラリがインストールされていません: {e}")
    print("このスクリプトはRaspberry Pi上で実行してください")


MEMORY_SIZE_CANDIDATES_GB = [1, 2, 4, 8, 16, 32]
DISPLAY_WINDOW_NAME = 'YOLO Benchmark'
DISPLAY_WINDOW_POS = (40, 40)
WARMUP_FRAMES = 1


class HailoTelemetryMonitor:
    """Hailo-10H 側の温度、RAM、NNC 使用率を測定する"""

    def __init__(
        self,
        enabled: bool = False,
        sampling_period_ms: int = 100,
        poll_interval_sec: float = 1.0,
    ):
        self.enabled = enabled
        self.sampling_period_ms = sampling_period_ms
        self.poll_interval_sec = poll_interval_sec
        self.measurements = []
        self.error = None
        self.device = None
        self.control = None
        self.has_performance_stats = False
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        if not enabled:
            return

        try:
            from hailo_platform import Device

            self.device = Device()
            self.control = self.device.control
            self.has_performance_stats = hasattr(self.control, "query_performance_stats")
            print("✓ HAT側テレメトリの初期化完了")
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"警告: HAT側テレメトリの初期化に失敗しました: {self.error}")

    def start(self):
        if not self.enabled or self.control is None or self._thread is not None:
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return

        self._stop_event.set()
        self._thread.join(timeout=max(1.0, self.poll_interval_sec + 0.5))
        self._thread = None

    def close(self):
        self.stop()
        if self.device is None:
            return
        try:
            self.device.release()
        except Exception:
            pass
        self.device = None

    def latest(self):
        with self._lock:
            if not self.measurements:
                return None
            return dict(self.measurements[-1])

    def snapshot(self):
        with self._lock:
            return [dict(measurement) for measurement in self.measurements]

    def _run(self):
        while not self._stop_event.is_set():
            self.measure()
            self._stop_event.wait(self.poll_interval_sec)

    @staticmethod
    def _read_attr(obj, name):
        try:
            return getattr(obj, name)
        except Exception:
            return None

    @staticmethod
    def _ram_kib_to_mb(value):
        if value is None or value < 0:
            return None
        return value / 1024

    def measure(self):
        if not self.enabled or self.control is None:
            return None

        record = {
            "timestamp": time.time(),
            "hailo_error": None,
        }

        try:
            temperature = self.control.get_chip_temperature()
            ts0 = self._read_attr(temperature, "ts0_temperature")
            ts1 = self._read_attr(temperature, "ts1_temperature")
            temps = [value for value in (ts0, ts1) if value is not None]
            record.update({
                "hailo_ts0_temp_c": ts0,
                "hailo_ts1_temp_c": ts1,
                "hailo_temp_c": float(np.mean(temps)) if temps else None,
            })
        except Exception as exc:  # noqa: BLE001
            record["hailo_error"] = f"temperature: {type(exc).__name__}: {exc}"

        if self.has_performance_stats:
            try:
                stats = self.control.query_performance_stats(self.sampling_period_ms)
                ram_total = self._read_attr(stats, "ram_size_total")
                ram_used = self._read_attr(stats, "ram_size_used")
                ram_total_mb = self._ram_kib_to_mb(ram_total)
                ram_used_mb = self._ram_kib_to_mb(ram_used)
                record.update({
                    "hailo_cpu_utilization": self._read_attr(stats, "cpu_utilization"),
                    "hailo_nnc_utilization": self._read_attr(stats, "nnc_utilization"),
                    "hailo_dsp_utilization": self._read_attr(stats, "dsp_utilization"),
                    "hailo_ram_total_mb": ram_total_mb,
                    "hailo_ram_used_mb": ram_used_mb,
                    "hailo_ram_percent": (
                        (ram_used_mb / ram_total_mb) * 100
                        if ram_total_mb and ram_used_mb is not None
                        else None
                    ),
                    "hailo_ddr_noc_total_transactions": self._read_attr(
                        stats, "ddr_noc_total_transactions"
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                existing = record.get("hailo_error")
                perf_error = f"performance_stats: {type(exc).__name__}: {exc}"
                record["hailo_error"] = f"{existing}; {perf_error}" if existing else perf_error

        with self._lock:
            self.measurements.append(record)
        return record

    def get_statistics(self):
        with self._lock:
            measurements = list(self.measurements)

        if not measurements:
            return {}

        numeric_keys = sorted({
            key
            for measurement in measurements
            for key, value in measurement.items()
            if isinstance(value, (int, float)) and key != "timestamp" and value >= 0
        })

        def summarize(values):
            return {
                "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                "min": float(np.min(values)),
                "max": float(np.max(values)),
                "p50": float(np.percentile(values, 50)),
                "p95": float(np.percentile(values, 95)),
                "p99": float(np.percentile(values, 99)),
            }

        stats = {}
        for key in numeric_keys:
            values = [
                measurement[key]
                for measurement in measurements
                if isinstance(measurement.get(key), (int, float)) and measurement[key] >= 0
            ]
            if values:
                stats[key] = summarize(values)

        stats["measurement_count"] = len(measurements)
        if self.error:
            stats["init_error"] = self.error
        return stats


def detect_host_memory_label() -> str:
    """Pi本体メモリ容量から、ファイル名に使うラベルを返す"""
    total_gib = psutil.virtual_memory().total / (1024 ** 3)
    detected_gb = min(MEMORY_SIZE_CANDIDATES_GB, key=lambda size: abs(size - total_gib))
    return f"{detected_gb}gb"


def inject_memory_label(filepath: str, memory_label: str) -> str:
    """出力ファイル名にメモリ容量ラベルを埋め込む"""
    path = Path(filepath).expanduser()
    normalized_stem = path.stem.lower()

    if memory_label in normalized_stem:
        return str(path)

    return str(path.with_name(f"{path.stem}_{memory_label}{path.suffix}"))


def flatten_hailo_telemetry(stats: dict) -> dict:
    """HAT側テレメトリの集計結果をCSVの1行へ展開する"""
    flattened = {}

    for metric, value in stats.items():
        if isinstance(value, dict):
            for key, metric_value in value.items():
                flattened[f"{metric}_{key}"] = metric_value
        elif isinstance(value, (int, float, str)):
            flattened[f"hailo_telemetry_{metric}"] = value

    return flattened


def calculate_stage_profile_stats(profiles: list) -> dict:
    """カメラ取得、Hailo実行、後処理などの区間別時間を集計する"""
    if not profiles:
        return {}

    numeric_keys = sorted({
        key
        for profile in profiles
        for key, value in profile.items()
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and key != "timestamp"
        )
    })

    def summarize(values):
        return {
            "mean": float(np.mean(values)),
            "std": float(np.std(values)),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)),
            "p99": float(np.percentile(values, 99)),
        }

    stats = {}
    for key in numeric_keys:
        values = [
            profile[key]
            for profile in profiles
            if (
                isinstance(profile.get(key), (int, float))
                and not isinstance(profile.get(key), bool)
            )
        ]
        if values:
            stats[key] = summarize(values)

    stats["measurement_count"] = len(profiles)
    return stats


def flatten_stage_profile(stats: dict) -> dict:
    """区間別プロファイルの集計結果をCSVの1行へ展開する"""
    flattened = {}

    for metric, value in stats.items():
        if isinstance(value, dict):
            for key, metric_value in value.items():
                flattened[f"stage_{metric}_{key}"] = metric_value
        elif isinstance(value, (int, float, str)):
            flattened[f"stage_profile_{metric}"] = value

    return flattened


class YOLOBenchmark:
    """YOLO物体検出のベンチマーククラス"""
    
    def __init__(
        self,
        model_path: str,
        resolution: tuple = (640, 640),
        hat_telemetry: bool = False,
        hat_telemetry_sampling_ms: int = 100,
        profile_stages: bool = False,
    ):
        """
        初期化
        
        Args:
            model_path: Hailoモデルファイルのパス
            resolution: 入力解像度 (width, height)
        """
        self.model_path = model_path
        self.resolution = resolution
        self.hailo_telemetry = HailoTelemetryMonitor()
        self.profile_stages = profile_stages
        
        print(f"モデルをロード中: {model_path}")
        print(f"解像度: {resolution[0]}x{resolution[1]}")
        
        # HailoRT Python API の初期化
        try:
            self.runner = HailoYoloRunner(model_path, profile_stages=profile_stages)
            print("✓ Hailo NPUの初期化完了")
        except Exception as e:
            print(f"✗ Hailo NPUの初期化失敗: {e}")
            raise

        self.hailo_telemetry = HailoTelemetryMonitor(
            enabled=hat_telemetry,
            sampling_period_ms=hat_telemetry_sampling_ms,
        )
        
        # カメラの初期化
        try:
            self.camera = Picamera2()
            config = self.camera.create_preview_configuration(
                main={"size": resolution, "format": "RGB888"}
            )
            self.camera.configure(config)
            self.camera.start()
            print("✓ カメラの初期化完了")
        except Exception as e:
            print(f"✗ カメラの初期化失敗: {e}")
            raise
        
        # COCO 80クラスの名前
        self.class_names = [
            'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train',
            'truck', 'boat', 'traffic light', 'fire hydrant', 'stop sign',
            'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep',
            'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella',
            'handbag', 'tie', 'suitcase', 'frisbee', 'skis', 'snowboard',
            'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard',
            'surfboard', 'tennis racket', 'bottle', 'wine glass', 'cup', 'fork',
            'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich', 'orange',
            'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair',
            'couch', 'potted plant', 'bed', 'dining table', 'toilet', 'tv',
            'laptop', 'mouse', 'remote', 'keyboard', 'cell phone', 'microwave',
            'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase',
            'scissors', 'teddy bear', 'hair drier', 'toothbrush'
        ]
    
    def run_benchmark(self, duration: int = 60, display: bool = False) -> dict:
        """
        ベンチマークを実行
        
        Args:
            duration: 測定時間（秒）
            display: 画面表示を行うか
        
        Returns:
            dict: ベンチマーク結果
        """
        print(f"\n=== ベンチマーク開始（{duration}秒間） ===\n")
        
        # 測定用オブジェクトの初期化
        fps_counter = FPSCounter()
        system_monitor = SystemMonitor()
        raw_latencies = []
        raw_detection_counts = []
        latencies = []
        detection_counts = []
        stage_profiles = []
        
        start_time = time.time()
        frame_count = 0
        
        try:
            self.hailo_telemetry.start()

            if display:
                cv2.namedWindow(DISPLAY_WINDOW_NAME, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(DISPLAY_WINDOW_NAME, self.resolution[0], self.resolution[1])
                cv2.moveWindow(DISPLAY_WINDOW_NAME, *DISPLAY_WINDOW_POS)

            while time.time() - start_time < duration:
                # フレーム取得
                frame_start = time.perf_counter()
                capture_start = time.perf_counter()
                frame = self.camera.capture_array()
                capture_ms = (time.perf_counter() - capture_start) * 1000
                
                # NPU推論
                detections = self.runner.infer(frame)
                
                # レイテンシ記録
                latency = (time.perf_counter() - frame_start) * 1000  # ms
                
                # 検出数記録
                valid_detections = [d for d in detections if d.get('confidence', 0) > 0.5]
                frame_count += 1

                raw_latencies.append(latency)
                raw_detection_counts.append(len(valid_detections))

                if frame_count <= WARMUP_FRAMES:
                    continue

                latencies.append(latency)
                detection_counts.append(len(valid_detections))

                if self.profile_stages:
                    runner_profile = dict(getattr(self.runner, "last_profile", {}))
                    stage_profiles.append({
                        "timestamp": time.time(),
                        "capture_ms": capture_ms,
                        "total_frame_ms": latency,
                        **runner_profile,
                    })
                
                # FPSカウント更新
                fps_counter.update()
                
                # システムリソース測定（10フレームごと）
                if fps_counter.frame_count % 10 == 0:
                    system_monitor.measure()
                
                # 30フレームごとに進捗表示
                if fps_counter.frame_count % 30 == 0:
                    current_fps = fps_counter.get_fps()
                    avg_latency = np.mean(latencies[-30:])
                    sys_info = system_monitor.measure()
                    hat_info = self.hailo_telemetry.latest()

                    status_parts = [f"メモリ {sys_info['memory_percent']:4.1f}%"]

                    cpu_temp = sys_info.get("cpu_temp")
                    if cpu_temp is not None:
                        status_parts.append(f"CPU温度 {cpu_temp:4.1f}C")

                    if hat_info:
                        hat_temp = hat_info.get("hailo_temp_c")
                        hat_ram = hat_info.get("hailo_ram_used_mb")
                        hat_ram_percent = hat_info.get("hailo_ram_percent")
                        if hat_temp is not None:
                            status_parts.append(f"HAT温度 {hat_temp:4.1f}C")
                        if hat_ram is not None and hat_ram_percent is not None:
                            status_parts.append(
                                f"HAT RAM {hat_ram:5.1f}MB/{hat_ram_percent:4.1f}%"
                            )
                    
                    print(f"フレーム {fps_counter.frame_count:4d}: "
                          f"FPS {current_fps:5.1f}, "
                          f"レイテンシ {avg_latency:5.1f}ms, "
                          f"{', '.join(status_parts)}")
                
                # 画面表示（オプション）
                if display:
                    self._draw_detections(frame, valid_detections)
                    cv2.imshow(DISPLAY_WINDOW_NAME, frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
        
        except KeyboardInterrupt:
            print("\n中断されました")
            raise
        
        finally:
            self.hailo_telemetry.stop()
            if display:
                cv2.destroyAllWindows()
        
        # 結果の集計
        elapsed_time = time.time() - start_time
        final_fps = fps_counter.get_fps()
        latency_stats = PerformanceMetrics.calculate_latency_stats(latencies)
        system_stats = system_monitor.get_statistics()
        hailo_telemetry_stats = self.hailo_telemetry.get_statistics()
        hailo_telemetry_measurements = self.hailo_telemetry.snapshot()
        stage_profile_stats = calculate_stage_profile_stats(stage_profiles)
        
        results = {
            'summary': {
                'duration_sec': elapsed_time,
                'total_frames': fps_counter.frame_count,
                'warmup_frames_skipped': WARMUP_FRAMES,
                'fps': final_fps,
                'avg_detections': np.mean(detection_counts),
                'host_memory_label': detect_host_memory_label(),
                'model': self.model_path,
                'resolution': f"{self.resolution[0]}x{self.resolution[1]}",
                'model_input_resolution': (
                    f"{self.runner.input_resolution[0]}x{self.runner.input_resolution[1]}"
                ),
                'resize_count': self.runner.resize_count,
                'no_resize_count': self.runner.no_resize_count,
            },
            'latency': latency_stats,
            'system': system_stats,
            'hailo_telemetry': hailo_telemetry_stats,
            'stage_profile': stage_profile_stats,
            'raw_data': {
                'latencies': latencies,
                'detection_counts': detection_counts,
                'raw_latencies': raw_latencies,
                'raw_detection_counts': raw_detection_counts,
                'system_measurements': system_monitor.measurements,
                'hailo_telemetry_measurements': hailo_telemetry_measurements,
                'stage_profiles': stage_profiles,
            }
        }
        
        # 結果表示
        print(f"\n=== ベンチマーク結果 ===")
        print(f"測定時間: {elapsed_time:.1f}秒")
        print(f"総フレーム数: {fps_counter.frame_count}")
        print(f"除外したウォームアップ: {WARMUP_FRAMES}フレーム")
        print(f"平均FPS: {final_fps:.2f}")
        print(f"平均レイテンシ: {latency_stats['mean_ms']:.2f}ms")
        print(f"P95レイテンシ: {latency_stats['p95_ms']:.2f}ms")
        print(f"平均検出数: {np.mean(detection_counts):.1f}個/フレーム")
        print(f"平均CPU使用率: {system_stats['cpu_percent']['mean']:.1f}%")
        print(f"平均メモリ使用量: {system_stats['memory_used_mb']['mean']:.1f}MB")
        print(f"平均メモリ使用率: {system_stats['memory_percent']['mean']:.1f}%")
        print(f"平均スワップ使用率: {system_stats['swap_percent']['mean']:.1f}%")
        print(f"平均スワップ使用量: {system_stats['swap_used_mb']['mean']:.1f}MB")
        if 'cpu_temp' in system_stats:
            print(f"平均CPU温度: {system_stats['cpu_temp']['mean']:.1f}C")
        if 'hailo_temp_c' in hailo_telemetry_stats:
            print(f"平均Hailo温度: {hailo_telemetry_stats['hailo_temp_c']['mean']:.1f}C")
        if 'hailo_ram_used_mb' in hailo_telemetry_stats:
            print(f"平均HAT RAM使用量: {hailo_telemetry_stats['hailo_ram_used_mb']['mean']:.1f}MB")
        if 'hailo_ram_percent' in hailo_telemetry_stats:
            print(f"平均HAT RAM使用率: {hailo_telemetry_stats['hailo_ram_percent']['mean']:.1f}%")
        if 'hailo_nnc_utilization' in hailo_telemetry_stats:
            print(f"平均NNC使用率: {hailo_telemetry_stats['hailo_nnc_utilization']['mean']:.1f}%")
        if stage_profile_stats:
            print("区間別平均時間:")
            for label, key in [
                ("カメラ取得", "capture_ms"),
                ("リサイズ", "resize_ms"),
                ("Hailo実行", "hailo_run_ms"),
                ("後処理", "parse_ms"),
                ("推論合計", "infer_total_ms"),
            ]:
                if key in stage_profile_stats:
                    print(f"  {label}: {stage_profile_stats[key]['mean']:.2f}ms")
        
        return results
    
    def _draw_detections(self, frame, detections):
        """
        検出結果をフレームに描画
        
        Args:
            frame: 画像フレーム
            detections: 検出結果のリスト
        """
        for det in detections:
            class_id = det.get('class_id', 0)
            confidence = det.get('confidence', 0)
            bbox = det.get('bbox', [0, 0, 0, 0])  # (x1, y1, x2, y2)
            
            # バウンディングボックス
            x1, y1, x2, y2 = map(int, bbox)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            
            # ラベル
            label = f"{self.class_names[class_id]}: {confidence:.2f}"
            cv2.putText(frame, label, (x1, y1 - 10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    
    def cleanup(self, close_runner: bool = True, show_message: bool = True):
        """リソースのクリーンアップ"""
        try:
            self.camera.stop()
            if show_message:
                print("✓ カメラを停止しました")
        except Exception:
            pass
        if close_runner:
            try:
                self.runner.close()
            except Exception:
                pass
        self.hailo_telemetry.close()


def main():
    """メイン関数"""
    parser = argparse.ArgumentParser(
        description='YOLO物体検出の性能測定スクリプト',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        '--model',
        type=str,
        default='/usr/share/hailo-models/yolov8m_h10.hef',
        help='Hailoモデルファイルのパス'
    )
    parser.add_argument(
        '--resolution',
        type=str,
        default='640x640',
        help='入力解像度（例: 640x640, 1280x720）'
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=60,
        help='測定時間（秒）'
    )
    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='結果の保存先（CSVファイル）'
    )
    parser.add_argument(
        '--display',
        action='store_true',
        help='画面表示を行う'
    )
    parser.add_argument(
        '--hat-telemetry',
        action='store_true',
        help='HAT側の温度、RAM、NNC使用率も測定する'
    )
    parser.add_argument(
        '--hat-telemetry-sampling-ms',
        type=int,
        default=100,
        help='HAT側テレメトリのサンプリング時間（ミリ秒）'
    )
    parser.add_argument(
        '--profile-stages',
        action='store_true',
        help='カメラ取得、Hailo実行、後処理の区間別時間も測定する'
    )
    
    args = parser.parse_args()
    
    # 解像度のパース
    try:
        width, height = map(int, args.resolution.split('x'))
        resolution = (width, height)
    except:
        print(f"エラー: 解像度の形式が不正です: {args.resolution}")
        sys.exit(1)
    
    # ベンチマーク実行
    benchmark = None
    try:
        benchmark = YOLOBenchmark(
            args.model,
            resolution,
            hat_telemetry=args.hat_telemetry,
            hat_telemetry_sampling_ms=args.hat_telemetry_sampling_ms,
            profile_stages=args.profile_stages,
        )
        results = benchmark.run_benchmark(args.duration, args.display)
        
        # 結果の保存
        if args.output:
            output_path = inject_memory_label(args.output, results['summary']['host_memory_label'])
            if output_path != args.output:
                print(
                    "保存ファイル名に本体メモリ容量を反映します: "
                    f"{args.output} -> {output_path}"
                )

            # サマリーをCSVに保存
            summary_data = [{
                'timestamp': time.time(),
                **results['summary'],
                **{f'latency_{k}': v for k, v in results['latency'].items()},
                **{f'cpu_{k}': v for k, v in results['system']['cpu_percent'].items()},
                **{f'memory_used_mb_{k}': v for k, v in results['system']['memory_used_mb'].items()},
                **{f'memory_{k}': v for k, v in results['system']['memory_percent'].items()},
                **{f'swap_percent_{k}': v for k, v in results['system']['swap_percent'].items()},
                **{f'swap_used_mb_{k}': v for k, v in results['system']['swap_used_mb'].items()},
                **(
                    {f'cpu_temp_{k}': v for k, v in results['system']['cpu_temp'].items()}
                    if 'cpu_temp' in results['system']
                    else {}
                ),
                **flatten_hailo_telemetry(results['hailo_telemetry']),
                **flatten_stage_profile(results['stage_profile']),
            }]
            DataSaver.save_to_csv(summary_data, output_path)
            
            # 詳細データをJSONに保存
            output_csv_path = Path(output_path)
            json_path = str(output_csv_path.with_name(f"{output_csv_path.stem}_detail.json"))
            DataSaver.save_to_json(results, json_path)
        
        benchmark.cleanup()
    
    except KeyboardInterrupt:
        if benchmark is not None:
            benchmark.cleanup(close_runner=False, show_message=False)
        sys.exit(130)
        
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

