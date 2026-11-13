#!/bin/bash
# 図面管理のバックアップ（毎晩 3:30、systemd の zumen-backup.timer から）
# - DB（sales.db・meishi.db）は sqlite3 の .backup で写しを取る（動いている最中でも壊れない取り方）
# - 原本のファイルは「足すだけ」で送る（VPS側で消しても退避先は消えない）
# - .env に R2 の鍵があれば R2 へ（DZ_BACKUP_PASSWORD があれば中身もファイル名も暗号化）。なければ VPS 内に退避
# - 結果は /var/meishi/backup-zumen/last.json に書き、図面タブの「設定」に出る
set -uo pipefail
BE=${BE:-/var/meishi/backend}
LOCAL=${DZ_BACKUP_DIR:-/var/meishi/backup-zumen}
ENVF=$BE/.env
if [ -f "$ENVF" ]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^(DZ_R2_[A-Z_]+|DZ_BACKUP_[A-Z_]+|DZ_FILES_DIR|SALES_DB_PATH|DB_PATH)=' "$ENVF" | sed 's/\r$//')
  set +a
fi
FILES=${DZ_FILES_DIR:-/var/meishi/files}
SALES_DB=${SALES_DB_PATH:-$BE/sales.db}
MEISHI_DB=${DB_PATH:-$BE/meishi.db}
DAY=$(date +%Y%m%d)
KEEP_DAYS=${DZ_BACKUP_KEEP_DAYS:-30}
mkdir -p "$LOCAL/db/$DAY" "$LOCAL/files"
STATUS=$LOCAL/last.json
MARK=$LOCAL/.last_ok
ERR=""
TARGET="VPS内のみ"

fail() { ERR="$1"; echo "!! $1" >&2; }

# 1. DB の写し
DBB=0
for db in "$SALES_DB" "$MEISHI_DB"; do
  [ -f "$db" ] || continue
  out="$LOCAL/db/$DAY/$(basename "$db")"
  if python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1], timeout=20); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$db" "$out"; then
    gzip -f "$out" && DBB=$((DBB + $(stat -c %s "$out.gz")))
  else
    fail "DBの写しを取れませんでした: $db"
  fi
done

# 2. 今回はじめて退避する原本の数（前回の成功より新しいもの）
if [ -f "$MARK" ]; then
  SENT=$(find "$FILES/orig" -type f ! -name '*.tmp' -newer "$MARK" 2>/dev/null | wc -l)
else
  SENT=$(find "$FILES/orig" -type f ! -name '*.tmp' 2>/dev/null | wc -l)
fi

# 3. 退避先へ
if [ -n "${DZ_R2_BUCKET:-}" ] && [ -n "${DZ_R2_ENDPOINT:-}" ] && [ -n "${DZ_R2_ACCESS_KEY_ID:-}" ] && [ -n "${DZ_R2_SECRET_ACCESS_KEY:-}" ]; then
  if ! command -v rclone >/dev/null; then
    fail "rclone が入っていません（sudo apt install rclone）"
  else
    export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_ENDPOINT="$DZ_R2_ENDPOINT" \
      RCLONE_CONFIG_R2_ACCESS_KEY_ID="$DZ_R2_ACCESS_KEY_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$DZ_R2_SECRET_ACCESS_KEY" RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
    PREFIX=${DZ_BACKUP_PREFIX:-backup}
    if [ -n "${DZ_BACKUP_PASSWORD:-}" ]; then
      export RCLONE_CONFIG_SAFE_TYPE=crypt RCLONE_CONFIG_SAFE_REMOTE="r2:$DZ_R2_BUCKET/$PREFIX" \
        RCLONE_CONFIG_SAFE_PASSWORD="$(rclone obscure "$DZ_BACKUP_PASSWORD")"
      DEST="safe:"; TARGET="R2（暗号化）"
    else
      DEST="r2:$DZ_R2_BUCKET/$PREFIX"; TARGET="R2（暗号化なし）"
    fi
    rclone copy "$LOCAL/db/$DAY" "$DEST/db/$DAY" --retries 5 || fail "DBをR2へ送れませんでした"
    if [ -d "$FILES/orig" ]; then
      rclone copy "$FILES/orig" "$DEST/files/orig" --exclude '*.tmp' --transfers 4 --retries 5 || fail "原本をR2へ送れませんでした"
    fi
    # R2 の DB は KEEP_DAYS 日ぶんと、毎月1日のものを12か月ぶん残す
    for d in $(rclone lsf "$DEST/db" --dirs-only 2>/dev/null | tr -d '/'); do
      [[ "$d" =~ ^[0-9]{8}$ ]] || continue
      age=$(( ( $(date +%s) - $(date -d "$d" +%s) ) / 86400 ))
      if [ "$age" -gt "$KEEP_DAYS" ] && { [ "${d:6:2}" != "01" ] || [ "$age" -gt 366 ]; }; then
        rclone purge "$DEST/db/$d" || true
      fi
    done
  fi
else
  # R2 がまだなら VPS 内に退避。原本は書き換えない作りなので、ハードリンクで「消しても残る」状態にする（容量は増えない）
  if [ -d "$FILES/orig" ]; then
    cp -alnr "$FILES/orig" "$LOCAL/files/" 2>/dev/null || cp -anr "$FILES/orig" "$LOCAL/files/" || fail "原本を退避できませんでした"
  fi
fi

# 4. 手元の DB の写しは 14日ぶんと毎月1日のもの
for d in $(ls "$LOCAL/db" 2>/dev/null); do
  [[ "$d" =~ ^[0-9]{8}$ ]] || continue
  age=$(( ( $(date +%s) - $(date -d "$d" +%s) ) / 86400 ))
  if [ "$age" -gt 14 ] && { [ "${d:6:2}" != "01" ] || [ "$age" -gt 366 ]; }; then rm -rf "${LOCAL:?}/db/$d"; fi
done

# 5. 結果
OK=true; [ -n "$ERR" ] && OK=false
python3 - "$STATUS" "$OK" "$TARGET" "$DBB" "$SENT" "$ERR" <<'PY'
import json, sys, datetime
p, ok, target, dbb, sent, err = sys.argv[1:7]
json.dump({"ok": ok == "true", "finished_at": datetime.datetime.now().isoformat(timespec="seconds"), "target": target,
           "db_bytes": int(dbb), "files_sent": int(sent), "error": err or None}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
chmod 644 "$STATUS"
if [ "$OK" = true ]; then
  touch "$MARK"
else
  if [ -n "${DZ_BACKUP_SLACK_WEBHOOK:-}" ]; then
    curl -s -X POST -H 'Content-Type: application/json' --data "{\"text\":\"図面のバックアップに失敗しました: $ERR\"}" "$DZ_BACKUP_SLACK_WEBHOOK" >/dev/null || true
  fi
  exit 1
fi
echo "バックアップ完了: $TARGET  DB $DBB bytes  新しい原本 $SENT 件"
