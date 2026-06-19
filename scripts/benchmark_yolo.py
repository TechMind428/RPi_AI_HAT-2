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


class YOLOBenchmark:
    """YOLO物体検出のベンチマーククラス"""
    
    def __init__(self, model_path: str, resolution: tuple = (640, 640)):
        """
        初期化
        
        Args:
            model_path: Hailoモデルファイルのパス
            resolution: 入力解像度 (width, height)
        """
        self.model_path = model_path
        self.resolution = resolution
        
        print(f"モデルをロード中: {model_path}")
        print(f"解像度: {resolution[0]}x{resolution[1]}")
        
        # HailoRT Python API の初期化
        try:
            self.runner = HailoYoloRunner(model_path)
            print("✓ Hailo NPUの初期化完了")
        except Exception as e:
            print(f"✗ Hailo NPUの初期化失敗: {e}")
            raise
        
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
        
        start_time = time.time()
        frame_count = 0
        
        try:
            if display:
                cv2.namedWindow(DISPLAY_WINDOW_NAME, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(DISPLAY_WINDOW_NAME, self.resolution[0], self.resolution[1])
                cv2.moveWindow(DISPLAY_WINDOW_NAME, *DISPLAY_WINDOW_POS)

            while time.time() - start_time < duration:
                # フレーム取得
                frame_start = time.time()
                frame = self.camera.capture_array()
                
                # NPU推論
                detections = self.runner.infer(frame)
                
                # レイテンシ記録
                latency = (time.time() - frame_start) * 1000  # ms
                
                # 検出数記録
                valid_detections = [d for d in detections if d.get('confidence', 0) > 0.5]
                frame_count += 1

                raw_latencies.append(latency)
                raw_detection_counts.append(len(valid_detections))

                if frame_count <= WARMUP_FRAMES:
                    continue

                latencies.append(latency)
                detection_counts.append(len(valid_detections))
                
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
                    
                    print(f"フレーム {fps_counter.frame_count:4d}: "
                          f"FPS {current_fps:5.1f}, "
                          f"レイテンシ {avg_latency:5.1f}ms, "
                          f"CPU {sys_info['cpu_percent']:4.1f}%, "
                          f"メモリ {sys_info['memory_percent']:4.1f}%")
                
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
            if display:
                cv2.destroyAllWindows()
        
        # 結果の集計
        elapsed_time = time.time() - start_time
        final_fps = fps_counter.get_fps()
        latency_stats = PerformanceMetrics.calculate_latency_stats(latencies)
        system_stats = system_monitor.get_statistics()
        
        results = {
            'summary': {
                'duration_sec': elapsed_time,
                'total_frames': fps_counter.frame_count,
                'warmup_frames_skipped': WARMUP_FRAMES,
                'fps': final_fps,
                'avg_detections': np.mean(detection_counts),
                'host_memory_label': detect_host_memory_label(),
                'model': self.model_path,
                'resolution': f"{self.resolution[0]}x{self.resolution[1]}"
            },
            'latency': latency_stats,
            'system': system_stats,
            'raw_data': {
                'latencies': latencies,
                'detection_counts': detection_counts,
                'raw_latencies': raw_latencies,
                'raw_detection_counts': raw_detection_counts,
                'system_measurements': system_monitor.measurements
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
        benchmark = YOLOBenchmark(args.model, resolution)
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
                )
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

# Made with Bob
