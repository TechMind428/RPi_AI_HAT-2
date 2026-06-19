#!/usr/bin/env python3
"""
Benchmark Utilities - 共通ユーティリティ関数

このモジュールは、性能測定に必要な共通機能を提供します：
- システムリソース測定（CPU、メモリ、温度）
- データ保存（CSV、JSON）
- 統計計算（平均、標準偏差、パーセンタイル）
- ログ記録
"""

import psutil
import time
import csv
import json
import numpy as np
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime


class SystemMonitor:
    """システムリソースを監視するクラス"""
    
    def __init__(self):
        """初期化"""
        self.measurements = []
    
    def measure(self) -> Dict[str, Any]:
        """
        システムリソースを測定
        
        Returns:
            Dict: システムリソース情報
        """
        # CPU使用率（0.1秒間隔で測定）
        cpu_percent = psutil.cpu_percent(interval=0.1)
        
        # メモリ情報
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        # 温度情報（利用可能な場合）
        try:
            temps = psutil.sensors_temperatures()
            cpu_entries = temps.get('cpu_thermal', [])
            if cpu_entries:
                first_entry = cpu_entries[0]
                cpu_temp = getattr(first_entry, 'current', None)
                if cpu_temp is None and isinstance(first_entry, dict):
                    cpu_temp = first_entry.get('current', None)
            else:
                cpu_temp = None
        except (AttributeError, KeyError):
            cpu_temp = None
        
        measurement = {
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'cpu_percent': cpu_percent,
            'memory_total_mb': mem.total / 1024 / 1024,
            'memory_used_mb': mem.used / 1024 / 1024,
            'memory_available_mb': mem.available / 1024 / 1024,
            'memory_percent': mem.percent,
            'swap_total_mb': swap.total / 1024 / 1024,
            'swap_used_mb': swap.used / 1024 / 1024,
            'swap_percent': swap.percent,
            'cpu_temp': cpu_temp
        }
        
        self.measurements.append(measurement)
        return measurement
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        測定データの統計情報を取得
        
        Returns:
            Dict: 統計情報
        """
        if not self.measurements:
            return {}

        def summarize(values: List[float]) -> Dict[str, float]:
            return {
                'mean': np.mean(values),
                'std': np.std(values),
                'min': np.min(values),
                'max': np.max(values),
                'p50': np.percentile(values, 50),
                'p95': np.percentile(values, 95),
                'p99': np.percentile(values, 99)
            }

        stats = {
            'cpu_percent': summarize([m['cpu_percent'] for m in self.measurements]),
            'memory_used_mb': summarize([m['memory_used_mb'] for m in self.measurements]),
            'memory_percent': summarize([m['memory_percent'] for m in self.measurements]),
            'swap_percent': summarize([m['swap_percent'] for m in self.measurements]),
            'swap_used_mb': summarize([m['swap_used_mb'] for m in self.measurements]),
            'measurement_count': len(self.measurements)
        }

        cpu_temps = [m['cpu_temp'] for m in self.measurements if m['cpu_temp'] is not None]
        if cpu_temps:
            stats['cpu_temp'] = summarize(cpu_temps)

        return stats
    
    def reset(self):
        """測定データをリセット"""
        self.measurements = []


class DataSaver:
    """測定データを保存するクラス"""
    
    @staticmethod
    def save_to_csv(data: List[Dict], filepath: str):
        """
        測定データをCSVファイルに保存
        
        Args:
            data: 保存するデータ（辞書のリスト）
            filepath: 保存先ファイルパス
        """
        if not data:
            print(f"警告: 保存するデータがありません")
            return
        
        # ディレクトリが存在しない場合は作成
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        
        # CSVに保存
        with open(filepath, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        
        print(f"データを保存しました: {filepath} ({len(data)}行)")
    
    @staticmethod
    def save_to_json(data: Any, filepath: str, indent: int = 2):
        """
        データをJSONファイルに保存
        
        Args:
            data: 保存するデータ
            filepath: 保存先ファイルパス
            indent: インデント幅
        """
        # ディレクトリが存在しない場合は作成
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        
        # JSONに保存
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        
        print(f"データを保存しました: {filepath}")
    
    @staticmethod
    def append_to_csv(data: Dict, filepath: str):
        """
        測定データをCSVファイルに追記
        
        Args:
            data: 追記するデータ（辞書）
            filepath: 保存先ファイルパス
        """
        # ディレクトリが存在しない場合は作成
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        
        # ファイルが存在するか確認
        file_exists = Path(filepath).exists()
        
        # CSVに追記
        with open(filepath, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=data.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)


class PerformanceMetrics:
    """性能メトリクスを計算するクラス"""
    
    @staticmethod
    def calculate_fps(frame_count: int, elapsed_time: float) -> float:
        """
        FPSを計算
        
        Args:
            frame_count: フレーム数
            elapsed_time: 経過時間（秒）
        
        Returns:
            float: FPS
        """
        if elapsed_time <= 0:
            return 0.0
        return frame_count / elapsed_time
    
    @staticmethod
    def calculate_latency_stats(latencies: List[float]) -> Dict[str, float]:
        """
        レイテンシの統計情報を計算
        
        Args:
            latencies: レイテンシのリスト（ミリ秒）
        
        Returns:
            Dict: 統計情報
        """
        if not latencies:
            return {}
        
        return {
            'mean_ms': np.mean(latencies),
            'std_ms': np.std(latencies),
            'min_ms': np.min(latencies),
            'max_ms': np.max(latencies),
            'p50_ms': np.percentile(latencies, 50),
            'p95_ms': np.percentile(latencies, 95),
            'p99_ms': np.percentile(latencies, 99)
        }
    
    @staticmethod
    def calculate_throughput(item_count: int, elapsed_time: float) -> float:
        """
        スループットを計算
        
        Args:
            item_count: 処理したアイテム数
            elapsed_time: 経過時間（秒）
        
        Returns:
            float: スループット（items/sec）
        """
        if elapsed_time <= 0:
            return 0.0
        return item_count / elapsed_time


class FPSCounter:
    """FPSをカウントするクラス"""
    
    def __init__(self):
        """初期化"""
        self.frame_count = 0
        self.start_time = time.time()
        self.last_report_time = self.start_time
        self.last_report_count = 0
    
    def update(self):
        """フレームカウントを更新"""
        self.frame_count += 1
    
    def get_fps(self) -> float:
        """
        現在のFPSを取得
        
        Returns:
            float: FPS
        """
        elapsed = time.time() - self.start_time
        return self.frame_count / elapsed if elapsed > 0 else 0.0
    
    def get_interval_fps(self, interval: float = 1.0) -> Optional[float]:
        """
        指定間隔のFPSを取得
        
        Args:
            interval: 測定間隔（秒）
        
        Returns:
            Optional[float]: FPS（間隔に達していない場合はNone）
        """
        current_time = time.time()
        elapsed = current_time - self.last_report_time
        
        if elapsed >= interval:
            frame_diff = self.frame_count - self.last_report_count
            fps = frame_diff / elapsed
            
            # 次回のために更新
            self.last_report_time = current_time
            self.last_report_count = self.frame_count
            
            return fps
        
        return None
    
    def reset(self):
        """カウンターをリセット"""
        self.frame_count = 0
        self.start_time = time.time()
        self.last_report_time = self.start_time
        self.last_report_count = 0


def format_bytes(bytes_value: int) -> str:
    """
    バイト数を人間が読みやすい形式に変換
    
    Args:
        bytes_value: バイト数
    
    Returns:
        str: フォーマットされた文字列
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"


def format_duration(seconds: float) -> str:
    """
    秒数を人間が読みやすい形式に変換
    
    Args:
        seconds: 秒数
    
    Returns:
        str: フォーマットされた文字列
    """
    if seconds < 60:
        return f"{seconds:.2f}秒"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.2f}分"
    else:
        hours = seconds / 3600
        return f"{hours:.2f}時間"


if __name__ == "__main__":
    # テスト実行
    print("=== Benchmark Utilities テスト ===\n")
    
    # SystemMonitorのテスト
    print("1. SystemMonitor テスト")
    monitor = SystemMonitor()
    for i in range(5):
        measurement = monitor.measure()
        print(f"  測定 {i+1}: CPU {measurement['cpu_percent']:.1f}%, "
              f"メモリ {measurement['memory_percent']:.1f}%")
        time.sleep(0.5)
    
    stats = monitor.get_statistics()
    print(f"\n  統計情報:")
    print(f"    CPU平均: {stats['cpu_percent']['mean']:.1f}%")
    print(f"    メモリ平均: {stats['memory_percent']['mean']:.1f}%")
    
    # FPSCounterのテスト
    print("\n2. FPSCounter テスト")
    fps_counter = FPSCounter()
    for i in range(30):
        fps_counter.update()
        time.sleep(0.033)  # 約30fps
        if (i + 1) % 10 == 0:
            print(f"  {i+1}フレーム: {fps_counter.get_fps():.1f} FPS")
    
    # DataSaverのテスト
    print("\n3. DataSaver テスト")
    test_data = [
        {'frame': 1, 'fps': 30.5, 'latency': 33.2},
        {'frame': 2, 'fps': 30.8, 'latency': 32.5},
        {'frame': 3, 'fps': 29.9, 'latency': 33.8}
    ]
    DataSaver.save_to_csv(test_data, 'test_output.csv')
    DataSaver.save_to_json({'test': 'data'}, 'test_output.json')
    
    print("\n=== テスト完了 ===")

# Made with Bob
