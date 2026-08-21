# RPi_AI_HAT-2

本リポジトリは、Interface 2026年10月号 特設記事で使用するサンプルコードと測定結果をまとめたものです。

[https://interface.cqpub.co.jp/magazine/202610/](https://interface.cqpub.co.jp/magazine/202610/)

記事でご案内の通り、Raspberry Pi側で本リポジトリを `git clone` し、`scripts/` 配下のファイルを `~/scripts/` にコピーして使うことを前提にしています。

## 使い方

まず、誌面第３章の説明に沿って Raspberry Pi OS、HailoRT、hailo-ollama、Python環境をセットアップしてください。その後、Raspberry Pi上で次のように取得します。

```bash
cd ~
git clone https://github.com/TechMind428/RPi_AI_HAT-2.git
mkdir -p ~/scripts
cp ~/RPi_AI_HAT-2/scripts/* ~/scripts/
chmod +x ~/scripts/*.sh
```

第4章以降では、`~/scripts/` に置いたスクリプトを使って、YOLO物体検出、LLM比較、マルチモーダル実験を行います。

## ディレクトリ構成

- `scripts/`: 記事で使用するPythonスクリプト、実行用シェル、プロンプト定義
- `results/`: 記事で参照する測定結果
- `experiments/`: 測定CSVの列説明などの補足資料

## 含めていないもの

本リポジトリには、HailoのDebianパッケージ、Python wheel、HEFモデルファイルは含めていません。これらは誌面の第3章の手順を順に進めることで、公式配布元またはaptから導入されます。

## 注意

このリポジトリ単体でセットアップ手順を完結させるものではありません。必ず Interface 2026年10月号 特設記事「ラズパイ X AI HAT+ 2で実験＆アプリケーション作り」の特に第3章の手順とあわせて利用してください。
