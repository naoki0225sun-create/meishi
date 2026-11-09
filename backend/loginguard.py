"""ログイン失敗の回数制限（名刺・営業アプリと業者画面で共通）

- 同じ相手（IP）が同じアカウントで 5 回まちがえると 15 分ロック
- 同じ IP が アカウントを変えながら 20 回まちがえても 15 分ロック
- アカウント全体（どの IP からでも）で 15 分に 20 回まちがえたら Slack に「攻撃の疑い」を通知（ロックはしない）
  ※ パスワードは会社で共通なので、アカウントごと止めると社員全員が入れなくなる。だから止めるのは相手の IP だけ
- 通知は同じ相手について 15 分に 1 回まで
- 記録はメモリ上のみ（再起動で消える。ロックが解けるだけなので安全側）
"""
import threading
import time

WINDOW = 15 * 60
PER_PAIR = 5      # 同じ IP × 同じアカウント
PER_IP = 20       # 同じ IP（アカウントを変えながら）
ACCOUNT_ALERT = 20  # アカウント全体（IP を変えながら）

HOOKS: list = []   # 通知先（slack_bot が登録する）。fn(info: dict)

_lock = threading.Lock()
_fails: dict = {}      # key -> [時刻...]
_notified: dict = {}   # key -> 最後に通知した時刻
_inflight: dict = {}   # key -> [時刻...] パスワードを確かめている最中の試行（同時に大量に送られても数えるため）
INFLIGHT_TTL = 60


def client_ip(request) -> str:
    """nginx の後ろで動く前提。nginx が足した X-Forwarded-For の右端だけを信じる"""
    ip = request.client.host if request and request.client else "-"
    xff = request.headers.get("x-forwarded-for") if request else None
    if ip in ("127.0.0.1", "::1"):
        if xff:
            ip = xff.split(",")[-1].strip() or ip
        elif request.headers.get("x-real-ip"):
            ip = request.headers.get("x-real-ip").strip()[:64] or ip
    return ip


def _prune(key: str, now: float) -> list:
    arr = [x for x in _fails.get(key, []) if now - x < WINDOW]
    if arr:
        _fails[key] = arr
    else:
        _fails.pop(key, None)
    return arr


def _wait_min(arr: list, now: float) -> int:
    return max(1, int((WINDOW - (now - min(arr))) // 60) + 1) if arr else 1


def _live(key: str, now: float) -> list:
    arr = [x for x in _inflight.get(key, []) if now - x < INFLIGHT_TTL]
    if arr:
        _inflight[key] = arr
    else:
        _inflight.pop(key, None)
    return arr


def check(scope: str, account: str, ip: str, reserve: bool = False):
    """ロック中なら (True, あと何分) を返す。
    reserve=True なら、ロックでなければ「確かめ中の1回」として先に数えておく（終わったら必ず release する）。
    パスワードの確認は時間がかかるので、同時に何十回も送られても上限を超えて試せないようにするため"""
    now = time.time()
    pk, ik = f"{scope}|{account}|{ip}", f"ip|{ip}"
    with _lock:
        pair = _prune(pk, now)
        byip = _prune(ik, now)
        if len(pair) + len(_live(pk, now)) >= PER_PAIR:
            return True, _wait_min(pair, now)
        if len(byip) + len(_live(ik, now)) >= PER_IP:
            return True, _wait_min(byip, now)
        if reserve:
            _inflight.setdefault(pk, []).append(now)
            _inflight.setdefault(ik, []).append(now)
    return False, 0


def release(scope: str, account: str, ip: str):
    """check(reserve=True) で数えた「確かめ中の1回」を外す"""
    now = time.time()
    with _lock:
        for k in (f"{scope}|{account}|{ip}", f"ip|{ip}"):
            arr = _live(k, now)
            if arr:
                arr.pop(0)
                if arr:
                    _inflight[k] = arr
                else:
                    _inflight.pop(k, None)


def _notify(info: dict):
    for fn in list(HOOKS):
        try:
            threading.Thread(target=fn, args=(info,), daemon=True).start()
        except Exception:
            pass


def fail(scope: str, account: str, ip: str, tenant_id: str = "", label: str = ""):
    """失敗を記録する。ロックや攻撃の疑いになったら通知"""
    now = time.time()
    events = []
    with _lock:
        pk, ik, ak = f"{scope}|{account}|{ip}", f"ip|{ip}", f"acct|{scope}|{account}"
        for k in (pk, ik, ak):
            _fails.setdefault(k, []).append(now)
            _prune(k, now)
        np_, ni = len(_fails.get(pk, [])), len(_fails.get(ik, []))
        if np_ >= PER_PAIR or ni >= PER_IP:
            nk = f"lock|{ip}"
            if now - _notified.get(nk, 0) > WINDOW:
                _notified[nk] = now
                by_ip = np_ < PER_PAIR   # アカウントを変えながらの失敗で止めた
                events.append({"kind": "lock", "scope": scope, "account": account, "label": label,
                               "tenant_id": tenant_id, "ip": ip, "count": ni if by_ip else np_, "by_ip": by_ip})
        if len(_fails.get(ak, [])) >= ACCOUNT_ALERT:
            nk = f"alert|{ak}"
            if now - _notified.get(nk, 0) > WINDOW:
                _notified[nk] = now
                ips = {k.split("|")[-1] for k in _fails if k.startswith(f"{scope}|{account}|")}
                events.append({"kind": "attack", "scope": scope, "account": account, "label": label,
                               "tenant_id": tenant_id, "ip": ip, "count": len(_fails[ak]), "ips": len(ips)})
        # 古いキーの掃除（メモリが増え続けないように）
        if len(_fails) > 5000:
            for k in list(_fails):
                _prune(k, now)
    for e in events:
        _notify(e)


def success(scope: str, account: str, ip: str):
    with _lock:
        _fails.pop(f"{scope}|{account}|{ip}", None)


def reset():
    with _lock:
        _fails.clear()
        _notified.clear()
        _inflight.clear()
