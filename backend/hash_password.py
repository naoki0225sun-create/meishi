#!/usr/bin/env python3
"""
bcrypt パスワードハッシュ生成ツール

使い方（VPS で実行）:
    sudo -u meishi /var/meishi/backend/venv/bin/python /var/meishi/backend/hash_password.py

対話的にパスワードを入力すると、bcryptハッシュを出力する。
出力されたハッシュを tenants.yaml の password_hash に貼り付ける。
"""
import getpass
import bcrypt

print("=== パスワードハッシュ生成 ===")
print("tenants.yaml の password_hash 欄に貼り付ける値を生成します。")
print()

while True:
    pw1 = getpass.getpass("パスワードを入力 (画面には表示されません): ")
    if len(pw1) < 8:
        print("⚠️ 8文字以上にしてください。")
        continue
    pw2 = getpass.getpass("もう一度入力: ")
    if pw1 != pw2:
        print("⚠️ 一致しません。最初からやり直します。")
        continue
    break

hashed = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
print()
print("=== 生成されたハッシュ（この値を tenants.yaml に貼り付け） ===")
print(hashed)
print()
print("例:")
print(f'    password_hash: "{hashed}"')
