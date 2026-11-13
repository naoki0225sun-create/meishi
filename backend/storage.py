"""
ファイルの保存先（図面管理）
============================
置く・読む・あるか・消す の4つだけを持つ口。中身はローカル版と R2 版。
.env の DZ_STORAGE=local|r2 で選ぶ（既定 local）。

置き場所のキーは中身のハッシュで決める（一度置いたものは書き換えない）。
  原本:   orig/<tenant>/<sha[:2]>/<sha><ext>
  画像:   page/<sha>/<n>-<size>.jpg   （作り直せるのでバックアップには含めない）
"""

import os
import shutil
from typing import Optional

ROOT = os.getenv("DZ_FILES_DIR", "/var/meishi/files")


def orig_key(tenant_id: str, sha: str, ext: str) -> str:
    ext = (ext or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return f"orig/{tenant_id}/{sha[:2]}/{sha}{ext[:8]}"


def page_key(sha: str, page_no: int, size: str) -> str:
    return f"page/{sha[:2]}/{sha}/{page_no}-{size}.jpg"


class LocalStore:
    kind = "local"

    def __init__(self, root: str = None):
        self.root = root or ROOT

    def _path(self, key: str) -> str:
        if ".." in key.split("/") or key.startswith("/"):
            raise ValueError("不正なキーです")
        return os.path.join(self.root, key)

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        if os.path.exists(p):
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)

    def get(self, key: str) -> Optional[bytes]:
        p = self._path(key)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def local_path(self, key: str) -> Optional[str]:
        p = self._path(key)
        return p if os.path.exists(p) else None

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def delete_prefix(self, prefix: str) -> None:
        p = self._path(prefix.rstrip("/"))
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)


class R2Store:
    """Cloudflare R2（S3互換）。画像のように何度も読むものは手元にも置く（cache）"""
    kind = "r2"

    def __init__(self):
        import boto3  # 使うときだけ
        self.bucket = os.environ["DZ_R2_BUCKET"]
        self.s3 = boto3.client(
            "s3", endpoint_url=os.environ["DZ_R2_ENDPOINT"],
            aws_access_key_id=os.environ["DZ_R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["DZ_R2_SECRET_ACCESS_KEY"], region_name="auto")
        self.cache = LocalStore(os.getenv("DZ_CACHE_DIR", ROOT))

    def put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        if key.startswith("page/"):
            self.cache.put(key, data)

    def get(self, key: str) -> Optional[bytes]:
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        try:
            data = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception:
            return None
        self.cache.put(key, data)
        return data

    def local_path(self, key: str) -> Optional[str]:
        if self.get(key) is None:
            return None
        return self.cache.local_path(key)

    def exists(self, key: str) -> bool:
        if self.cache.exists(key):
            return True
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.cache.delete(key)
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def delete_prefix(self, prefix: str) -> None:
        self.cache.delete_prefix(prefix)


_STORE = {}


def get_store():
    kind = os.getenv("DZ_STORAGE", "local")
    if kind not in _STORE:
        _STORE[kind] = R2Store() if kind == "r2" else LocalStore()
    return _STORE[kind]


def reset():
    _STORE.clear()


def disk_usage(path: str = None) -> dict:
    """保存先のディスクの使用状況（R2 でも手元のディスクを見る）"""
    p = path or ROOT
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    u = shutil.disk_usage(p or "/")
    return {"total": u.total, "used": u.used, "free": u.free, "ratio": round(u.used / u.total, 3) if u.total else 0}
