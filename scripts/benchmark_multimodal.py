#!/usr/bin/env python3
"""
Multimodal AI Benchmark - マルチモーダルAI性能測定スクリプト

このスクリプトは、物体検出とLLM推論を統合したマルチモーダルAIシステムの性能を測定します：
- エンドツーエンド処理時間
- 各ステージの処理時間（物体検出、プロンプト生成、LLM推論）
- 並行処理の可否
- システムリソース使用率
- 説明の品質
"""

import argparse
import time
import sys
from pathlib import Path
import numpy as np
import json

# 共通ユーティリティをインポート
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from benchmark_utils import SystemMonitor, DataSaver, PerformanceMetrics
except ModuleNotFoundError as exc:
    if exc.name == "benchmark_utils":
        print("エラー: benchmark_utils.py が見つかりません")
        print(f"期待する場所: {SCRIPT_DIR / 'benchmark_utils.py'}")
        print("scripts ディレクトリ一式を同じ場所に置いたまま実行してください")
        raise SystemExit(1)
    raise

try:
    import hailo
    import hailo_llm
    from picamera2 import Picamera2
    import cv2
except ImportError as e:
    print(f"警告: 必要なライブラリがインストールされていません: {e}")
    print("このスクリプトはRaspberry Pi上で実行してください")


class MultimodalBenchmark:
    """マルチモーダルAIのベンチマーククラス"""
    
    def __init__(self, yolo_model_path: str, llm_model_path: str, resolution: tuple = (640, 640)):
        """
        初期化
        
        Args:
            yolo_model_path: YOLOモデルファイルのパス
            llm_model_path: LLMモデルファイルのパス
            resolution: カメラ解像度 (width, height)
        """
        self.resolution = resolution
        
        print(f"=== マルチモーダルAIシステム初期化 ===")
        print(f"YOLOモデル: {yolo_model_path}")
        print(f"LLMモデル: {llm_model_path}")
        print(f"解像度: {resolution[0]}x{resolution[1]}\n")
        
        # Hailo NPU（YOLO）の初期化
        try:
            self.yolo_device = hailo.Device()
            self.yolo_model = self.yolo_device.load_model(yolo_model_path)
            print("✓ YOLO物体検出モデルのロード完了")
        except Exception as e:
            print(f"✗ YOLOモデルのロード失敗: {e}")
            raise
        
        # Hailo LLM Runtimeの初期化
        try:
            self.llm = hailo_llm.LLM(
                model_path=llm_model_path,
                max_tokens=256,
                temperature=0.7
            )
            print("✓ LLMモデルのロード完了")
        except Exception as e:
            print(f"✗ LLMモデルのロード失敗: {e}")
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
        
        print("\n✓ マルチモーダルAIシステムの初期化完了\n")
    
    def detect_objects(self, frame):
        """
        物体検出を実行
        
        Args:
            frame: 画像フレーム
        
        Returns:
            tuple: (検出結果, 処理時間)
        """
        start_time = time.time()
        detections = self.yolo_model.infer(frame)
        elapsed = (time.time() - start_time) * 1000  # ms
        
        # 信頼度0.5以上の検出のみ
        valid_detections = [d for d in detections if d.get('confidence', 0) > 0.5]
        
        return valid_detections, elapsed
    
    def generate_prompt(self, detections):
        """
        検出結果からプロンプトを生成
        
        Args:
            detections: 検出結果のリスト
        
        Returns:
            tuple: (プロンプト, 処理時間)
        """
        start_time = time.time()
        
        if not detections:
            prompt = "画像には何も検出されませんでした。"
        else:
            # 検出されたオブジェクトをカウント
            object_counts = {}
            for det in detections:
                class_id = det.get('class_id', 0)
                class_name = self.class_names[class_id]
                object_counts[class_name] = object_counts.get(class_name, 0) + 1
            
            # プロンプト生成
            objects_str = ', '.join([f"{count}個の{name}" for name, count in object_counts.items()])
            prompt = f"画像には{objects_str}が検出されました。この状況を簡潔に説明してください。"
        
        elapsed = (time.time() - start_time) * 1000  # ms
        return prompt, elapsed
    
    def generate_explanation(self, prompt):
        """
        LLMで説明を生成
        
        Args:
            prompt: プロンプト
        
        Returns:
            tuple: (説明文, 処理時間, TTFT)
        """
        start_time = time.time()
        first_token_time = None
        tokens = []
        
        try:
            for token in self.llm.generate(prompt, max_new_tokens=100, stream=True):
                if first_token_time is None:
                    first_token_time = time.time()
                tokens.append(token)
        except Exception as e:
            print(f"エラー: LLM生成中に問題が発生しました: {e}")
            return "", 0, 0
        
        end_time = time.time()
        
        total_time = (end_time - start_time) * 1000  # ms
        ttft = (first_token_time - start_time) * 1000 if first_token_time else 0  # ms
        explanation = ''.join(tokens)
        
        return explanation, total_time, ttft
    
    def process_frame(self, frame):
        """
        1フレームを処理（物体検出 → プロンプト生成 → LLM推論）
        
        Args:
            frame: 画像フレーム
        
        Returns:
            dict: 処理結果と各ステージの処理時間
        """
        total_start = time.time()
        
        # ステージ1: 物体検出
        detections, detection_time = self.detect_objects(frame)
        
        # ステージ2: プロンプト生成
        prompt, prompt_time = self.generate_prompt(detections)
        
        # ステージ3: LLM推論
        explanation, llm_time, ttft = self.generate_explanation(prompt)
        
        total_time = (time.time() - total_start) * 1000  # ms
        
        result = {
            'detections': detections,
            'detection_count': len(detections),
            'prompt': prompt,
            'explanation': explanation,
            'timings': {
                'detection_ms': detection_time,
                'prompt_ms': prompt_time,
                'llm_ms': llm_time,
                'llm_ttft_ms': ttft,
                'total_ms': total_time
            }
        }
        
        return result
    
    def run_benchmark(self, duration: int = 60, parallel: bool = False) -> dict:
        """
        ベンチマークを実行
        
        Args:
            duration: 測定時間（秒）
            parallel: 並行処理を試行するか
        
        Returns:
            dict: ベンチマーク結果
        """
        print(f"\n=== マルチモーダルベンチマーク開始（{duration}秒間） ===")
        print(f"並行処理: {'有効' if parallel else '無効'}\n")
        
        system_monitor = SystemMonitor()
        results = []
        
        start_time = time.time()
        iteration = 0
        
        try:
            while time.time() - start_time < duration:
                iteration += 1
                
                # フレーム取得
                frame = self.camera.capture_array()
                
                # 処理実行
                result = self.process_frame(frame)
                results.append(result)
                
                # システムリソース測定
                sys_info = system_monitor.measure()
                
                # 進捗表示
                print(f"[{iteration}] 検出: {result['detection_count']}個, "
                      f"処理時間: {result['timings']['total_ms']:.0f}ms "
                      f"(検出:{result['timings']['detection_ms']:.0f}ms + "
                      f"LLM:{result['timings']['llm_ms']:.0f}ms), "
                      f"メモリ: {sys_info['memory_percent']:.1f}%")
                
                if result['explanation']:
                    print(f"     説明: {result['explanation'][:80]}...")
        
        except KeyboardInterrupt:
            print("\n中断されました")
        
        # 結果の集計
        if not results:
            print("\n警告: 有効な結果がありません")
            return {}
        
        detection_times = [r['timings']['detection_ms'] for r in results]
        llm_times = [r['timings']['llm_ms'] for r in results]
        total_times = [r['timings']['total_ms'] for r in results]
        detection_counts = [r['detection_count'] for r in results]
        
        system_stats = system_monitor.get_statistics()
        
        summary = {
            'total_iterations': len(results),
            'duration_sec': time.time() - start_time,
            'throughput': len(results) / (time.time() - start_time),
            'detection': {
                'mean_ms': np.mean(detection_times),
                'std_ms': np.std(detection_times),
                'p95_ms': np.percentile(detection_times, 95)
            },
            'llm': {
                'mean_ms': np.mean(llm_times),
                'std_ms': np.std(llm_times),
                'p95_ms': np.percentile(llm_times, 95)
            },
            'end_to_end': {
                'mean_ms': np.mean(total_times),
                'std_ms': np.std(total_times),
                'min_ms': np.min(total_times),
                'max_ms': np.max(total_times),
                'p95_ms': np.percentile(total_times, 95)
            },
            'detections': {
                'mean_count': np.mean(detection_counts),
                'total_count': sum(detection_counts)
            },
            'system': system_stats
        }
        
        # 結果表示
        print(f"\n=== ベンチマーク結果 ===")
        print(f"総イテレーション数: {summary['total_iterations']}")
        print(f"測定時間: {summary['duration_sec']:.1f}秒")
        print(f"スループット: {summary['throughput']:.2f} iterations/sec")
        print(f"\n処理時間:")
        print(f"  物体検出: {summary['detection']['mean_ms']:.1f}ms (P95: {summary['detection']['p95_ms']:.1f}ms)")
        print(f"  LLM推論: {summary['llm']['mean_ms']:.1f}ms (P95: {summary['llm']['p95_ms']:.1f}ms)")
        print(f"  エンドツーエンド: {summary['end_to_end']['mean_ms']:.1f}ms (P95: {summary['end_to_end']['p95_ms']:.1f}ms)")
        print(f"\n検出:")
        print(f"  平均検出数: {summary['detections']['mean_count']:.1f}個/フレーム")
        print(f"  総検出数: {summary['detections']['total_count']}個")
        print(f"\nシステムリソース:")
        print(f"  平均CPU使用率: {summary['system']['cpu_percent']['mean']:.1f}%")
        print(f"  平均メモリ使用率: {summary['system']['memory_percent']['mean']:.1f}%")
        
        return {
            'summary': summary,
            'details': results
        }
    
    def cleanup(self):
        """リソースのクリーンアップ"""
        try:
            self.camera.stop()
            print("✓ カメラを停止しました")
        except:
            pass


def main():
    """メイン関数"""
    parser = argparse.ArgumentParser(
        description='マルチモーダルAIの性能測定スクリプト'
    )
    parser.add_argument(
        '--yolo-model',
        type=str,
        default='yolov8n_hailo.hef',
        help='YOLOモデルファイルのパス'
    )
    parser.add_argument(
        '--llm-model',
        type=str,
        default='granite-3.3-2b_4bit_hailo.hef',
        help='LLMモデルファイルのパス'
    )
    parser.add_argument(
        '--resolution',
        type=str,
        default='640x640',
        help='カメラ解像度（例: 640x640）'
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=60,
        help='測定時間（秒）'
    )
    parser.add_argument(
        '--parallel',
        action='store_true',
        help='並行処理を試行'
    )
    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='結果の保存先（CSVファイル）'
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
    try:
        benchmark = MultimodalBenchmark(args.yolo_model, args.llm_model, resolution)
        results = benchmark.run_benchmark(args.duration, args.parallel)
        
        # 結果の保存
        if args.output and results:
            # サマリーをCSVに保存
            summary_data = [{
                'timestamp': time.time(),
                **results['summary'],
                **{f'detection_{k}': v for k, v in results['summary']['detection'].items()},
                **{f'llm_{k}': v for k, v in results['summary']['llm'].items()},
                **{f'e2e_{k}': v for k, v in results['summary']['end_to_end'].items()},
                **{f'detections_{k}': v for k, v in results['summary']['detections'].items()}
            }]
            DataSaver.save_to_csv(summary_data, args.output)
            
            # 詳細データをJSONに保存
            json_path = args.output.replace('.csv', '_detail.json')
            DataSaver.save_to_json(results, json_path)
        
        benchmark.cleanup()
        
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

# Made with Bob
