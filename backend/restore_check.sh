#!/bin/bash
# 「本当に戻せるか」の確認（毎月1日 4:30）。最新のDBの写しを開いて件数を数え、原本を20件選んで中身が一致するか確かめる
set -uo pipefail
umask 077   # 写しは root と本体だけが読めるように（同じVPSのほかのアプリから読めないように）
BE=${BE:-/var/meishi/backend}
LOCAL=${DZ_BACKUP_DIR:-/var/meishi/backup-zumen}
ENVF=$BE/.env
if [ -f "$ENVF" ]; then set -a; source <(grep -E '^(DZ_R2_[A-Z_]+|DZ_BACKUP_[A-Z_]+|DZ_FILES_DIR)=' "$ENVF" | sed 's/\r$//'); set +a; fi
FILES=${DZ_FILES_DIR:-/var/meishi/files}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
OUT=$LOCAL/restore.json
ERR=""; DOCS=0; CHECKED=0; BAD=0
USE_R2=false
if [ -n "${DZ_R2_BUCKET:-}" ] && command -v rclone >/dev/null; then
  USE_R2=true
  export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_ENDPOINT="$DZ_R2_ENDPOINT" \
    RCLONE_CONFIG_R2_ACCESS_KEY_ID="$DZ_R2_ACCESS_KEY_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$DZ_R2_SECRET_ACCESS_KEY" RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
  PREFIX=${DZ_BACKUP_PREFIX:-backup}
  if [ -n "${DZ_BACKUP_PASSWORD:-}" ]; then
    export RCLONE_CONFIG_SAFE_TYPE=crypt RCLONE_CONFIG_SAFE_REMOTE="r2:$DZ_R2_BUCKET/$PREFIX" RCLONE_CONFIG_SAFE_PASSWORD="$(rclone obscure "$DZ_BACKUP_PASSWORD")"
    DEST="safe:"
  else
    DEST="r2:$DZ_R2_BUCKET/$PREFIX"
  fi
  LATEST=$(rclone lsf "$DEST/db" --dirs-only | tr -d '/' | sort | tail -1)
  rclone copy "$DEST/db/$LATEST" "$TMP/db" || ERR="R2からDBを取り出せません"
else
  LATEST=$(ls "$LOCAL/db" | sort | tail -1)
  cp -r "$LOCAL/db/$LATEST" "$TMP/db" || ERR="DBの写しがありません"
fi
if [ -z "$ERR" ] && [ -f "$TMP/db/sales.db.gz" ]; then
  gunzip "$TMP/db/sales.db.gz"
  RES=$(python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); ok=c.execute("PRAGMA integrity_check").fetchone()[0]; n=c.execute("SELECT COUNT(*) FROM dz_docs").fetchone()[0] if ok=="ok" else 0; print(ok, n)' "$TMP/db/sales.db" 2>/dev/null || echo "ng 0")
  [ "${RES%% *}" = "ok" ] || ERR="DBの写しが壊れています"
  DOCS=${RES##* }
fi
for f in $(find "$FILES/orig" -type f ! -name '*.tmp' 2>/dev/null | shuf -n 20); do
  rel=${f#"$FILES"/}
  want=$(basename "$f"); want=${want%%.*}
  if $USE_R2; then got=$(rclone cat "$DEST/files/$rel" 2>/dev/null | sha256sum | cut -d' ' -f1)
  else got=$(sha256sum "$LOCAL/files/$rel" 2>/dev/null | cut -d' ' -f1); fi
  CHECKED=$((CHECKED + 1)); [ "$got" = "$want" ] || BAD=$((BAD + 1))
done
[ "$BAD" -gt 0 ] && ERR="${ERR:+$ERR / }退避先の原本が${BAD}件一致しません"
python3 - "$OUT" "$LATEST" "$DOCS" "$CHECKED" "$BAD" "$ERR" <<'PY'
import json, sys, datetime
p, latest, docs, checked, bad, err = sys.argv[1:7]
json.dump({"ok": not err, "at": datetime.datetime.now().isoformat(timespec="seconds"), "db": latest, "docs": int(docs or 0),
           "files_checked": int(checked), "files_bad": int(bad), "error": err or None}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
[ -z "$ERR" ] && echo "戻せることを確かめました（DB $LATEST・書類 $DOCS 件・原本 $CHECKED 件）" || { echo "!! $ERR"; exit 1; }
