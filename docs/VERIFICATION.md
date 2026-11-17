# 検証結果 — 2026-11-17

## 環境

Linux / Python3.12.14 / Node24.22.1 / FFmpeg7.1.5。DockerfileはNode22・Python3.12ベース。固定ライブラリはrequirementsとpackage-lock参照。

## 自動検証

- `python -m pytest backend/tests -q`: **6 passed**。
- `npm run typecheck`: 成功。
- `npm run lint`: 成功。
- `npm test`: **6 passed**。
- `npm run build`: TypeScript＋Vite＋Tailwind本番コンパイル成功。
- 独立Reviewerも上記を再実行。

バックエンド検証内容：Suno楽曲URL検証、CSRF・外部Origin拒否、他セッションのジョブ参照拒否、期限切れ削除、生成WAVのHTTP保存、同時処理制限と中止、完了イベント競合、Render HTTPS Origin、chunked入力サイズ制限、不正Content-Length。

暗号化オフラインfixture：生成AAC/M4AをAES-CTRで暗号化し、テスト専用のGCM権利応答を作成。元のMango実装による復号・FFprobe検証・M4A/MP3/WAVの実変換を全3形式で確認。fixtureは本番のSuno取得の代わりに使用されません。

## ブラウザと実Suno

統括担当が管理されたデスクトップChromeで確認：

- React＋Tailwind画面の表示、URL不正時の検証、M4A選択。
- 共有URLから実楽曲タイトル「Late Tokyo Drive」を表示。
- 実取得処理の中止後、入力画面へ復帰。
- 別ジョブで実音源の取得・処理・M4A保存準備完了まで到達。
- 「スマホに保存」のクリックでChromeのダウンロードイベントと保存ファイルの実パスを確認。
- 出力をFFprobeで確認：3,520,587 bytes、198.92秒、音声Opus、M4A/MP4コンテナ。

この実曲確認は1曲・M4A・デスクトップChromeのみ。実曲は納品ZIPへ含めない。ブラウザで保存ファイルが取得されたことと、Android/iOS実機でのファイル保存・再生は区別する。

## 独立レビュー

docs/REVIEW.md参照。High3件とMedium指摘を修正し、回帰を再実行。検証した範囲でCONDITIONAL PASS。ブラウザ実接続の結果はレビュー後に統括が追記したもの。

## 未検証・制約

- Render本番デプロイ・Dockerイメージビルドは未実施。
- Windows実機のBAT起動、Android/iOS実機の保存・再生は未実施。
- 全Suno楽曲・全配信形式への互換性は未確認。
- MP3/WAVの実Suno曲出力は未確認（オフライン実変換は成功）。
- 大容量・長時間負荷・大量利用の性能測定は未実施。
- Render無料枠の再起動・休止でジョブと一時ファイルが失われる。ライブラリ保存機能はない。
