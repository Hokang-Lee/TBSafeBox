
# TBSafeBox  Backup/Restore Patch (v3.4, Health Check + Retention 完全版)

**新機能**
- ✅ リモート共有の健全性チェック（SSH）
  - 未マウント検知（`mount`）
  - 書込み可否（`test -w`）
  - 空き容量チェック（`df`）最低空きGBを閾値で判定
- ♻️ 世代管理（リモート / Windows ローカル両方）
  - `epms-*.tar.zst` を **最新N世代保持**、または **日数保持**（どちらか/両方指定可）
  - 対応する `.sha256` / `.list` も連動削除
- 🔁 自動コピーは v3.3.1 の堅牢方式（直近パスをそのままコピー）を継続
- 🔎 Verify/Restore は直近自動選択（v3.1継続）

## 使い方（最短）
1. 依存インストール：`pip install paramiko`
2. ルートで起動：`python -m scripts.example_main`
3. SSH設定・Pathsを入力
4. **Health Check** と **Retention** を必要に応じて ON／パラメータ設定
5. **Run Backup** 実行 → 自動コピー → リテンション適用

## 設定項目（GUI）
- Health Check
  - Enable health check（ONで有効）
  - Require mount（`mount | grep <dest>` に一致しない場合は中断）
  - Min free (GB)（`df` の空きがこの値未満なら中断）
- Retention
  - Remote keep N / keep days
  - Local keep N / keep days
  （空欄または0は無効）

