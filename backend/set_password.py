"""名刺・営業アプリのログインパスワードを新しくする（VPS の上で実行する）

使い方:
  sudo /var/meishi/backend/venv/bin/python /var/meishi/backend/set_password.py           … 会社の一覧を出す
  sudo /var/meishi/backend/venv/bin/python /var/meishi/backend/set_password.py company_a … company_a のパスワードを変える

- 20文字のパスワードを作り、tenants.yaml の password_hash だけを書き換える（ほかの行・コメントはそのまま）
- 書き換える前に tenants.yaml.bak-日時 を残す
- 新しいパスワードはこの画面に1回だけ表示する（どこにも保存しない）
- 変えたあと `sudo systemctl restart meishi-backend` で全員がログアウトされ、新しいパスワードが必要になる
"""
import os
import re
import secrets
import shutil
import sys
from datetime import datetime

import bcrypt
import yaml

TENANTS_FILE = os.getenv("TENANTS_FILE", "/var/meishi/backend/tenants.yaml")
# 見まちがえやすい文字（0 O o 1 l I）は使わない
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def new_password(n: int = 20) -> str:
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(n))
        if re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"[0-9]", pw):
            return pw


def replace_hash(text: str, tenant_id: str, new_hash: str) -> str:
    lines = text.split("\n")
    start = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)(['\"]?)" + re.escape(tenant_id) + r"\2\s*:\s*(#.*)?$", ln)
        if m:
            start, base = i, len(m.group(1))
            break
    if start is None:
        raise SystemExit(f"tenants.yaml に {tenant_id} が見つかりません")
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln.lstrip().startswith("#") and len(ln) - len(ln.lstrip()) <= base:
            break   # この会社のブロックが終わった
        m = re.match(r"^(\s*)password_hash\s*:", ln)
        if m:
            lines[j] = f'{m.group(1)}password_hash: "{new_hash}"'
            return "\n".join(lines)
    raise SystemExit(f"{tenant_id} の password_hash の行が見つかりません")


def main():
    with open(TENANTS_FILE, encoding="utf-8") as f:
        text = f.read()
    tenants = (yaml.safe_load(text) or {}).get("tenants", {})
    if len(sys.argv) < 2:
        print("会社の一覧（この名前を付けて実行してください）:")
        for k, v in tenants.items():
            print(f"  {k}  {(v or {}).get('name', '')}")
        return
    tid = sys.argv[1]
    if tid not in tenants:
        raise SystemExit(f"{tid} という会社はありません。引数なしで実行すると一覧が出ます")
    pw = new_password()
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()
    new_text = replace_hash(text, tid, h)
    # 書き換え後に読み直して確かめる（壊れていたら書かない）
    chk = (yaml.safe_load(new_text) or {}).get("tenants", {})
    if set(chk) != set(tenants) or not bcrypt.checkpw(pw.encode(), str(chk[tid].get("password_hash", "")).encode()):
        raise SystemExit("書き換えの確認に失敗したので何も変更していません")
    for k in tenants:
        if k != tid and chk[k] != tenants[k]:
            raise SystemExit("ほかの会社の設定が変わってしまうため中止しました")
    bak = f"{TENANTS_FILE}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(TENANTS_FILE, bak)
    os.chmod(bak, 0o600)
    with open(TENANTS_FILE, "w", encoding="utf-8") as f:   # 上書き（持ち主・権限はそのまま）
        f.write(new_text)
    print()
    print(f"  {tid} の新しいパスワード:  {pw}")
    print()
    print("  ・この画面にしか出ません。社員に伝えたら画面を閉じてください（チャットやメールに貼らない）")
    print(f"  ・元のファイルは {bak} に残しました")
    print("  ・反映して全員をログアウトさせるには:  sudo systemctl restart meishi-backend")


if __name__ == "__main__":
    main()
