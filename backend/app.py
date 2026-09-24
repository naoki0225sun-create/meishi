"""
名刺管理ウェブアプリ バックエンド (v6.7)
=======================================
- v6.4 の全機能
- + 両面PDF対応
  - POST /api/upload/image に side=front/back フィールド対応
    ({card_id}_front.jpg / {card_id}_back.jpg で保存、side未指定は {card_id}.jpg で旧互換)
  - GET /api/cards/{card_id}/image に ?side=back 追加
    (未指定時は front → 旧 の順でフォールバック)
  - POST /api/cards/{card_id}/swap-images: 表裏ファイルをアトミックに入れ替え
  - has_image / has_back_image を fetch_cards 応答に含める
"""

import os
import re
import time
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import yaml
import bcrypt
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, UploadFile, File, Form, Header, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv
from PIL import Image
import io

import db
import dupes
import mailer

load_dotenv()

TENANTS_FILE = os.getenv("TENANTS_FILE", "/var/meishi/backend/tenants.yaml")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/var/meishi/backend/credentials.json")
FRONTEND_DIR = os.getenv("FRONTEND_DIR", "/var/meishi/frontend")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
UPLOAD_API_KEY = os.getenv("UPLOAD_API_KEY", "")  # scannerからのアップロード認証用
IMAGES_DIR = os.getenv("IMAGES_DIR", "/var/meishi/images")
SESSION_COOKIE_NAME = "meishi_session"
SESSION_DURATION_HOURS = 24 * 7
CACHE_DURATION_SECONDS = 300

active_sessions: dict[str, dict] = {}
_cache: dict[str, dict] = {}


def load_tenants() -> dict:
    if not os.path.exists(TENANTS_FILE):
        raise RuntimeError(f"テナント設定ファイルが見つかりません: {TENANTS_FILE}")
    with open(TENANTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("tenants", {})


def get_tenant(tenant_id: str) -> Optional[dict]:
    return load_tenants().get(tenant_id)


app = FastAPI(title="名刺管理アプリ v5")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    db.init_db()
    os.makedirs(IMAGES_DIR, exist_ok=True)


class LoginRequest(BaseModel):
    tenant_id: str
    password: str


class NoteUpdate(BaseModel):
    note: str


class TagAdd(BaseModel):
    tag: str


class FieldUpdate(BaseModel):
    value: str


class MergeRequest(BaseModel):
    kept_id: str
    removed_id: str
    field_choices: dict[str, str]  # {field: "kept" | "removed"}


class DismissRequest(BaseModel):
    card_id_a: str
    card_id_b: str


class EmailSendItem(BaseModel):
    card_id: str
    to_email: str
    subject: str
    body: str


class EmailBulkRequest(BaseModel):
    items: list[EmailSendItem]
    from_local_part: Optional[str] = None  # @より前の部分 (未指定時はテナントの既定を使う)
    from_name: Optional[str] = None         # 表示名 (未指定時はテナントの既定を使う)


class EmailPresetCreate(BaseModel):
    label: str
    local_part: str
    from_name: str
    signature: str = ""


class EmailPresetUpdate(BaseModel):
    label: str
    local_part: str
    from_name: str
    signature: str = ""


class CardDeleteRequest(BaseModel):
    card_ids: list[str]


def check_session(session_token: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME)) -> dict:
    if not session_token:
        raise HTTPException(status_code=401, detail="未ログイン")
    sess = active_sessions.get(session_token)
    if not sess:
        raise HTTPException(status_code=401, detail="セッションが無効")
    if datetime.now() > sess["expires_at"]:
        active_sessions.pop(session_token, None)
        raise HTTPException(status_code=401, detail="セッション期限切れ")
    sess["expires_at"] = datetime.now() + timedelta(hours=SESSION_DURATION_HOURS)
    return sess


def normalize_company(name: str) -> str:
    name = (name or "").strip()
    name = name.replace("(", "(").replace(")", ")")
    name = re.sub(r"\s*(株式会社|有限会社|合同会社|株\)|㈱)\s*", "", name)
    name = re.sub(r"\s*(Inc\.|Corp\.|Co\.,?\s*Ltd\.?|LLC|Ltd\.?)\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", "", name)
    return name.lower()


def clean_field(value: str) -> str:
    if not value:
        return ""
    v = value.strip()
    v = re.sub(r'^[\s/／,、]+', '', v)
    v = re.sub(r'[\s/／,、]+$', '', v)
    v = re.sub(r'\s{2,}', ' ', v)
    v = re.sub(r'\s*/\s*', ' / ', v)
    return v.strip()


def make_card_id(tenant_id: str, company: str, name: str) -> str:
    key = f"{tenant_id}|{normalize_company(company)}|{(name or '').strip()}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def get_sheets_service():
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def image_path_for(tenant_id: str, card_id: str, side: Optional[str] = None) -> str:
    """画像のファイルパス。
    side="front" → {card_id}_front.jpg
    side="back"  → {card_id}_back.jpg
    side=None    → {card_id}.jpg  (v6.4以前の旧形式)
    """
    if side == "front":
        fname = f"{card_id}_front.jpg"
    elif side == "back":
        fname = f"{card_id}_back.jpg"
    else:
        fname = f"{card_id}.jpg"
    return os.path.join(IMAGES_DIR, tenant_id, fname)


def resolve_image_path(tenant_id: str, card_id: str, side: Optional[str] = None) -> Optional[str]:
    """画像ファイルを実在確認した上でパスを返す。
    side指定あり → そのサイドのみ (存在しなければ None)
    side指定なし → front → 旧({card_id}.jpg) → back の順でフォールバック
    """
    if side in ("front", "back"):
        p = image_path_for(tenant_id, card_id, side)
        return p if os.path.exists(p) else None

    for s in ("front", None, "back"):
        p = image_path_for(tenant_id, card_id, s)
        if os.path.exists(p):
            return p
    return None


def has_image(tenant_id: str, card_id: str) -> bool:
    """画像ファイル（front / 旧 / back のいずれか）が存在するか"""
    return resolve_image_path(tenant_id, card_id) is not None


def has_back_image(tenant_id: str, card_id: str) -> bool:
    """裏面画像（_back.jpg）が存在するか"""
    return os.path.exists(image_path_for(tenant_id, card_id, "back"))


def fetch_cards(tenant_id: str) -> list[dict]:
    now = datetime.now()
    cached = _cache.get(tenant_id)
    if cached and cached.get("expires_at") and now < cached["expires_at"]:
        base_cards = cached["data"]
    else:
        tenant = get_tenant(tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail=f"テナントが見つかりません: {tenant_id}")
        spreadsheet_id = tenant.get("spreadsheet_id")
        sheet_name = tenant.get("sheet_name", "名刺")
        if not spreadsheet_id:
            raise HTTPException(status_code=500, detail=f"スプレッドシートIDが設定されていません: {tenant_id}")

        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A2:K",
        ).execute()
        rows = result.get("values", [])

        base_cards = []
        for row in rows:
            row = row + [""] * (11 - len(row))
            company_raw = row[0] or "不明"
            name_raw = row[1] or ""
            base_cards.append({
                "id": make_card_id(tenant_id, company_raw, name_raw),
                "company_raw": company_raw,
                "company": clean_field(company_raw) or "不明",
                "company_normalized": normalize_company(company_raw),
                "name_raw": name_raw,
                "name": clean_field(name_raw),
                "department_position_raw": row[2] or "",
                "department_position": clean_field(row[2] or ""),
                "email_raw": row[3] or "",
                "email": clean_field(row[3] or ""),
                "address_raw": row[4] or "",
                "address": clean_field(row[4] or ""),
                "mobile_raw": row[5] or "",
                "mobile": clean_field(row[5] or ""),
                "phone_raw": row[6] or "",
                "phone": clean_field(row[6] or ""),
                "website_raw": row[7] or "",
                "website": clean_field(row[7] or ""),
                "registered_date": row[8] or "",
                "assigned_to": clean_field(row[9] or ""),   # J列: 所有者（カンマ区切りで複数可）
                "assigned_to_list": [x.strip() for x in (row[9] or "").split(",") if x.strip()],  # パース済みリスト
                "name_kana": clean_field(row[10] or ""),    # K列: ふりがな (v6.9)
            })
        _cache[tenant_id] = {"data": base_cards, "expires_at": now + timedelta(seconds=CACHE_DURATION_SECONDS)}

    annotations = db.get_all_annotations(tenant_id)
    merged_removed = db.get_merged_removed(tenant_id)

    result = []
    for bc in base_cards:
        if bc["id"] in merged_removed:
            continue  # マージで消された側はスキップ
        c = dict(bc)
        ann = annotations.get(c["id"], {"note": "", "tags": [], "overrides": {}})
        c["note"] = ann["note"]
        c["tags"] = ann["tags"]
        overrides = ann.get("overrides", {})
        c["overrides"] = overrides
        for field, val in overrides.items():
            if field in db.EDITABLE_FIELDS:
                c[field] = val
        if "company" in overrides:
            c["company_normalized"] = normalize_company(c["company"])
        c["has_image"] = has_image(tenant_id, c["id"])
        c["has_back_image"] = has_back_image(tenant_id, c["id"])
        result.append(c)

    return result


# ── APIエンドポイント ──
@app.get("/api/tenants")
def list_tenants_public():
    tenants = load_tenants()
    return {
        "tenants": [
            {
                "id": tid,
                "display_name": t.get("display_name", tid),
                "accent_color": t.get("accent_color", "#2556E8"),
                "logo_initial": t.get("logo_initial", t.get("display_name", tid)[:1]),
            }
            for tid, t in tenants.items()
        ]
    }


@app.post("/api/login")
def login(req: LoginRequest, response: Response):
    tenant = get_tenant(req.tenant_id)
    if not tenant:
        import time; time.sleep(0.5)
        raise HTTPException(status_code=401, detail="認証に失敗しました")

    password_hash = tenant.get("password_hash", "")
    if not password_hash or password_hash.startswith("★") or password_hash.startswith("VPS"):
        raise HTTPException(status_code=500, detail="パスワードが未設定のテナントです")

    if not bcrypt.checkpw(req.password.encode("utf-8"), password_hash.encode("utf-8")):
        import time; time.sleep(0.5)
        raise HTTPException(status_code=401, detail="認証に失敗しました")

    token = secrets.token_urlsafe(32)
    active_sessions[token] = {
        "tenant_id": req.tenant_id,
        "expires_at": datetime.now() + timedelta(hours=SESSION_DURATION_HOURS),
    }
    response.set_cookie(
        key=SESSION_COOKIE_NAME, value=token,
        max_age=SESSION_DURATION_HOURS * 3600,
        httponly=True, samesite="lax", secure=True,
    )
    return {"ok": True, "tenant_id": req.tenant_id}


@app.post("/api/logout")
def logout(response: Response, session_token: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME)):
    if session_token:
        active_sessions.pop(session_token, None)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
def me(session: dict = Depends(check_session)):
    tenant = get_tenant(session["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=500, detail="テナント情報の取得に失敗")
    from_email = tenant.get("from_email", "") or ""
    # info@wakou-dk.com → local_part=info, domain=wakou-dk.com
    from_local_part = ""
    from_domain = ""
    if "@" in from_email:
        from_local_part, from_domain = from_email.split("@", 1)
    return {
        "authenticated": True,
        "tenant": {
            "id": session["tenant_id"],
            "display_name": tenant.get("display_name"),
            "accent_color": tenant.get("accent_color", "#2556E8"),
            "logo_initial": tenant.get("logo_initial", tenant.get("display_name", "")[:1]),
            "from_email": from_email,
            "from_local_part": from_local_part,
            "from_domain": from_domain,
            "from_name": tenant.get("from_name", ""),
            "can_send_email": bool(RESEND_API_KEY and from_email),
        },
    }


@app.get("/api/cards")
def list_cards(
    q: str = "",
    company: str = "",
    tag: str = "",
    assigned_to: str = "",  # 所有者フィルター (v6.9)
    session: dict = Depends(check_session),
):
    tenant_id = session["tenant_id"]
    cards = fetch_cards(tenant_id)

    if q:
        q_lower = q.lower()
        cards = [
            c for c in cards
            if q_lower in c["company"].lower()
            or q_lower in c["name"].lower()
            or q_lower in c["department_position"].lower()
            or q_lower in c["email"].lower()
            or q_lower in (c.get("note") or "").lower()
            or any(q_lower in t.lower() for t in c.get("tags", []))
            or any(q_lower in a.lower() for a in c.get("assigned_to_list", []))
            or q_lower in (c.get("name_kana") or "").lower()
        ]

    if company:
        cards = [c for c in cards if c["company_normalized"] == company]

    if tag:
        cards = [c for c in cards if tag in c.get("tags", [])]

    if assigned_to:
        # カンマ区切りの各所有者に対して部分一致フィルター
        cards = [c for c in cards if any(
            assigned_to.strip().lower() == a.lower()
            for a in c.get("assigned_to_list", [])
        )]

    groups: dict[str, dict] = {}
    for c in cards:
        key = c["company_normalized"]
        if key not in groups:
            groups[key] = {
                "company": c["company"],
                "company_normalized": key,
                "cards": [],
            }
        groups[key]["cards"].append(c)

    grouped = sorted(groups.values(), key=lambda g: g["company_normalized"])
    return {"total": len(cards), "groups": grouped}


@app.get("/api/companies")
def list_companies(session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    cards = fetch_cards(tenant_id)
    seen = {}
    for c in cards:
        if c["company_normalized"] not in seen:
            seen[c["company_normalized"]] = c["company"]
    return {"companies": [{"normalized": k, "display": v} for k, v in sorted(seen.items())]}


@app.get("/api/tags")
def list_tags(session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    return {"tags": db.list_all_tags(tenant_id)}


@app.post("/api/refresh")
def refresh_cache(session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    _cache.pop(tenant_id, None)
    cards = fetch_cards(tenant_id)
    return {"ok": True, "count": len(cards)}


# ── メモ ──
@app.get("/api/cards/{card_id}/note")
def get_note_api(card_id: str, session: dict = Depends(check_session)):
    return {"note": db.get_note(session["tenant_id"], card_id)}


@app.put("/api/cards/{card_id}/note")
def set_note_api(card_id: str, body: NoteUpdate, session: dict = Depends(check_session)):
    if len(body.note) > 10000:
        raise HTTPException(status_code=400, detail="メモは10000文字以下にしてください")
    db.set_note(session["tenant_id"], card_id, body.note)
    return {"ok": True}


# ── タグ ──
@app.get("/api/cards/{card_id}/tags")
def get_tags_api(card_id: str, session: dict = Depends(check_session)):
    return {"tags": db.get_tags(session["tenant_id"], card_id)}


@app.post("/api/cards/{card_id}/tags")
def add_tag_api(card_id: str, body: TagAdd, session: dict = Depends(check_session)):
    tag = body.tag.strip().lstrip("#")
    if not tag:
        raise HTTPException(status_code=400, detail="タグが空です")
    if len(tag) > 40:
        raise HTTPException(status_code=400, detail="タグは40文字以下にしてください")
    ok = db.add_tag(session["tenant_id"], card_id, tag)
    return {"ok": ok, "tag": tag}


@app.delete("/api/cards/{card_id}/tags/{tag}")
def delete_tag_api(card_id: str, tag: str, session: dict = Depends(check_session)):
    db.remove_tag(session["tenant_id"], card_id, tag)
    return {"ok": True}


# ── フィールド編集 ──
@app.put("/api/cards/{card_id}/field/{field}")
def set_field_api(
    card_id: str, field: str, body: FieldUpdate,
    session: dict = Depends(check_session),
):
    if field not in db.EDITABLE_FIELDS:
        raise HTTPException(status_code=400, detail=f"編集できない項目です: {field}")
    value = clean_field(body.value) if field != "note" else body.value
    if len(value) > 2000:
        raise HTTPException(status_code=400, detail="値が長すぎます")
    tenant_id = session["tenant_id"]
    db.set_override(tenant_id, card_id, field, value)

    # assigned_to はスプシの J 列にも直接書き戻す (v6.8)
    if field == "assigned_to":
        _write_assigned_to_sheet(tenant_id, card_id, value)
        _cache.pop(tenant_id, None)

    return {"ok": True, "field": field, "value": value}


def _write_assigned_to_sheet(tenant_id: str, card_id: str, value: str):
    """担当者をスプシの J 列に書き込む"""
    tenant = get_tenant(tenant_id)
    if not tenant:
        return
    spreadsheet_id = tenant.get("spreadsheet_id")
    sheet_name = tenant.get("sheet_name", "名刺")
    if not spreadsheet_id:
        return
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A2:B",
        ).execute()
        rows = result.get("values", [])
        # card_id と一致する行番号を探す
        for i, row in enumerate(rows):
            padded = row + [""] * (2 - len(row))
            company_raw = padded[0] or "不明"
            name_raw = padded[1] or ""
            if make_card_id(tenant_id, company_raw, name_raw) == card_id:
                row_num = i + 2  # 1-indexed + ヘッダー行分
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!J{row_num}",
                    valueInputOption="RAW",
                    body={"values": [[value]]},
                ).execute()
                break
    except Exception:
        pass  # スプシ書き込み失敗は無視（override は SQLite に保存済み）


@app.delete("/api/cards/{card_id}/field/{field}")
def reset_field_api(card_id: str, field: str, session: dict = Depends(check_session)):
    if field not in db.EDITABLE_FIELDS:
        raise HTTPException(status_code=400, detail=f"編集できない項目です: {field}")
    db.clear_override(session["tenant_id"], card_id, field)
    return {"ok": True}


@app.delete("/api/cards/{card_id}/overrides")
def reset_all_fields_api(card_id: str, session: dict = Depends(check_session)):
    db.clear_all_overrides(session["tenant_id"], card_id)
    return {"ok": True}


# ── 重複検出・マージ ──
@app.get("/api/duplicates")
def list_duplicates(session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    cards = fetch_cards(tenant_id)
    dismissed = db.get_dismissed_pairs(tenant_id)
    pairs = dupes.find_duplicates(cards, dismissed)
    return {"pairs": pairs, "count": len(pairs)}


@app.post("/api/duplicates/merge")
def merge_duplicates(req: MergeRequest, session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    cards = fetch_cards(tenant_id)
    card_map = {c["id"]: c for c in cards}

    if req.kept_id not in card_map or req.removed_id not in card_map:
        raise HTTPException(status_code=404, detail="カードが見つかりません")

    if req.kept_id == req.removed_id:
        raise HTTPException(status_code=400, detail="同じカードを指定できません")

    kept_card = card_map[req.kept_id]
    removed_card = card_map[req.removed_id]

    # 選択された値を kept_id のオーバーライドとして保存
    merged_values = dupes.merge_cards(kept_card, removed_card, req.field_choices)
    for field, value in merged_values.items():
        if field in db.EDITABLE_FIELDS and value:
            # 現在の表示値と同じならオーバーライドしない
            if value != kept_card.get(field, ""):
                db.set_override(tenant_id, req.kept_id, field, value)

    # メモのマージ: 両方あれば連結
    kept_note = db.get_note(tenant_id, req.kept_id)
    removed_note = db.get_note(tenant_id, req.removed_id)
    if removed_note.strip():
        if kept_note.strip() and removed_note.strip() not in kept_note:
            merged_note = kept_note + "\n\n---\n" + removed_note
        elif not kept_note.strip():
            merged_note = removed_note
        else:
            merged_note = kept_note
        db.set_note(tenant_id, req.kept_id, merged_note)

    # タグのマージ
    removed_tags = db.get_tags(tenant_id, req.removed_id)
    for t in removed_tags:
        db.add_tag(tenant_id, req.kept_id, t)

    # マージ記録
    db.record_merge(tenant_id, req.removed_id, req.kept_id)

    return {"ok": True}


@app.post("/api/duplicates/dismiss")
def dismiss_duplicate(req: DismissRequest, session: dict = Depends(check_session)):
    """「別人として扱う」"""
    tenant_id = session["tenant_id"]
    db.dismiss_pair(tenant_id, req.card_id_a, req.card_id_b)
    return {"ok": True}


# ── メール配信 ──
@app.post("/api/email/send-bulk")
def send_bulk_email(req: EmailBulkRequest, session: dict = Depends(check_session)):
    """差し込みメール一括配信"""
    tenant_id = session["tenant_id"]
    tenant = get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=500, detail="テナント情報エラー")

    tenant_from_email = tenant.get("from_email", "") or ""
    tenant_from_name = tenant.get("from_name", "") or ""

    if not RESEND_API_KEY:
        raise HTTPException(status_code=500, detail="メール送信機能が無効です (RESEND_API_KEYが未設定)")
    if not tenant_from_email or "@" not in tenant_from_email:
        raise HTTPException(status_code=500, detail="送信元メールアドレスが未設定です (tenants.yaml の from_email)")

    # ドメインはテナント既定値から固定、local_part は上書き可能
    _, tenant_domain = tenant_from_email.split("@", 1)

    # クライアントが送信元をカスタマイズした場合
    req_local = (req.from_local_part or "").strip()
    if req_local:
        # セキュリティ: local_part に @ や空白やタブが混入していないか
        if "@" in req_local or any(ch.isspace() for ch in req_local) or len(req_local) > 64:
            raise HTTPException(status_code=400, detail="送信元のアドレスが不正です")
        from_email = f"{req_local}@{tenant_domain}"
    else:
        from_email = tenant_from_email

    req_name = (req.from_name or "").strip()
    from_name = req_name if req_name else tenant_from_name
    if len(from_name) > 128:
        raise HTTPException(status_code=400, detail="送信者名が長すぎます")

    if len(req.items) > 500:
        raise HTTPException(status_code=400, detail="1回の送信は500件までです")

    # Resend のレート制限 (2通/秒) に対する安全マージン
    # 最後の1件の後は待たない
    SEND_INTERVAL_SECONDS = 0.6

    results = []
    total = len(req.items)
    for idx, item in enumerate(req.items):
        ok, provider_id, error = mailer.send_email(
            api_key=RESEND_API_KEY,
            from_email=from_email,
            from_name=from_name,
            to_email=item.to_email,
            subject=item.subject,
            body=item.body,
            reply_to=from_email,
        )
        status = "sent" if ok else "failed"
        db.log_email(
            tenant_id, item.card_id, item.to_email, item.subject,
            status, provider_id, error
        )
        results.append({
            "card_id": item.card_id,
            "to": item.to_email,
            "ok": ok,
            "error": error,
        })
        # 最後以外は次の送信までウェイト
        if idx < total - 1:
            time.sleep(SEND_INTERVAL_SECONDS)

    sent_count = sum(1 for r in results if r["ok"])
    return {
        "ok": True,
        "total": len(results),
        "sent": sent_count,
        "failed": len(results) - sent_count,
        "results": results,
    }


# ── プリセットAPI ──
@app.get("/api/email/presets")
def list_presets(session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    return {"presets": db.list_email_presets(tenant_id)}


@app.post("/api/email/presets")
def create_preset(req: EmailPresetCreate, session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    label = req.label.strip()
    local_part = req.local_part.strip()
    from_name = req.from_name.strip()
    signature = (req.signature or "").rstrip()
    if not label or not local_part or not from_name:
        raise HTTPException(status_code=400, detail="すべての項目を入力してください")
    if "@" in local_part or any(ch.isspace() for ch in local_part) or len(local_part) > 64:
        raise HTTPException(status_code=400, detail="アドレスの @ より前の部分が不正です")
    if len(label) > 40 or len(from_name) > 128:
        raise HTTPException(status_code=400, detail="入力が長すぎます")
    if len(signature) > 2000:
        raise HTTPException(status_code=400, detail="署名が長すぎます (2000文字以下)")
    pid = db.create_email_preset(tenant_id, label, local_part, from_name, signature)
    return {"ok": True, "id": pid}


@app.put("/api/email/presets/{preset_id}")
def update_preset(preset_id: int, req: EmailPresetUpdate, session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    label = req.label.strip()
    local_part = req.local_part.strip()
    from_name = req.from_name.strip()
    signature = (req.signature or "").rstrip()
    if not label or not local_part or not from_name:
        raise HTTPException(status_code=400, detail="すべての項目を入力してください")
    if "@" in local_part or any(ch.isspace() for ch in local_part) or len(local_part) > 64:
        raise HTTPException(status_code=400, detail="アドレスの @ より前の部分が不正です")
    if len(label) > 40 or len(from_name) > 128:
        raise HTTPException(status_code=400, detail="入力が長すぎます")
    if len(signature) > 2000:
        raise HTTPException(status_code=400, detail="署名が長すぎます (2000文字以下)")
    ok = db.update_email_preset(tenant_id, preset_id, label, local_part, from_name, signature)
    if not ok:
        raise HTTPException(status_code=404, detail="プリセットが見つかりません")
    return {"ok": True}


@app.delete("/api/email/presets/{preset_id}")
def remove_preset(preset_id: int, session: dict = Depends(check_session)):
    tenant_id = session["tenant_id"]
    ok = db.delete_email_preset(tenant_id, preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="プリセットが見つかりません")
    return {"ok": True}


@app.get("/api/email/logs")
def list_email_logs(
    card_id: Optional[str] = None,
    limit: int = 100,
    session: dict = Depends(check_session),
):
    tenant_id = session["tenant_id"]
    logs = db.get_email_logs(tenant_id, card_id=card_id, limit=min(limit, 500))
    return {"logs": logs}


# ── 画像アップロード・配信 ──
MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20MB
THUMB_MAX_WIDTH = 1200
THUMB_JPEG_QUALITY = 85


def _authenticate_upload(x_upload_key: Optional[str]):
    if not UPLOAD_API_KEY:
        raise HTTPException(status_code=503, detail="画像アップロード機能が無効 (UPLOAD_API_KEY未設定)")
    if not x_upload_key or not secrets.compare_digest(x_upload_key, UPLOAD_API_KEY):
        raise HTTPException(status_code=401, detail="アップロード認証エラー")


@app.post("/api/upload/image")
async def upload_image(
    tenant_id: str = Form(...),
    card_id: str = Form(...),
    file: UploadFile = File(...),
    side: Optional[str] = Form(None),  # v6.7: "front" / "back" / None(旧互換)
    x_upload_key: Optional[str] = Header(None, alias="X-Upload-Key"),
):
    """scanner (Mac) からの画像アップロード受け口。
    Tenant認証はAPIキー方式（セッション不要）。画像はJPEGにリサイズして保存。
    v6.7: side=front → {card_id}_front.jpg, side=back → {card_id}_back.jpg,
          side未指定 → {card_id}.jpg (旧互換)
    """
    _authenticate_upload(x_upload_key)

    if not get_tenant(tenant_id):
        raise HTTPException(status_code=404, detail=f"テナントが見つかりません: {tenant_id}")

    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', card_id):
        raise HTTPException(status_code=400, detail="card_id の形式が不正です")

    # side のバリデーション
    if side is not None and side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="side は 'front' または 'back' のみ指定可能です")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="ファイルが空です")
    if len(raw) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="ファイルが大きすぎます (20MB以下)")

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"画像として読み込めません: {e}")

    # EXIF回転を適用
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if img.mode != "RGB":
        img = img.convert("RGB")

    # サムネイル化（幅が大きい場合のみ縮小）
    if img.width > THUMB_MAX_WIDTH:
        ratio = THUMB_MAX_WIDTH / img.width
        new_h = int(img.height * ratio)
        img = img.resize((THUMB_MAX_WIDTH, new_h), Image.LANCZOS)

    tenant_dir = os.path.join(IMAGES_DIR, tenant_id)
    os.makedirs(tenant_dir, exist_ok=True)
    dst_path = image_path_for(tenant_id, card_id, side)

    # アトミック書き込み
    tmp_path = dst_path + ".tmp"
    img.save(tmp_path, "JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)
    os.replace(tmp_path, dst_path)

    size = os.path.getsize(dst_path)
    return {
        "ok": True,
        "card_id": card_id,
        "side": side,
        "size": size,
        "width": img.width,
        "height": img.height,
    }


@app.get("/api/cards/{card_id}/image")
def get_card_image(
    card_id: str,
    side: Optional[str] = Query(None),  # v6.7: "front" / "back" / None
    session: dict = Depends(check_session),
):
    """画像配信（セッション必須）。
    v6.7: ?side=back で裏面画像、?side=front で表面画像を取得。
          side未指定の場合は front → 旧 の順でフォールバック。
    """
    tenant_id = session["tenant_id"]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', card_id):
        raise HTTPException(status_code=400, detail="card_id の形式が不正です")
    if side is not None and side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="side は 'front' または 'back' のみ指定可能です")

    path = resolve_image_path(tenant_id, card_id, side)
    if not path:
        raise HTTPException(status_code=404, detail="画像がありません")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/cards/{card_id}/swap-images")
def swap_card_images(card_id: str, session: dict = Depends(check_session)):
    """表裏画像の入れ替え (v6.7)。_front.jpg と _back.jpg を一時ファイル経由でスワップ。
    片方しか存在しない場合はエラーを返す。
    """
    tenant_id = session["tenant_id"]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', card_id):
        raise HTTPException(status_code=400, detail="card_id の形式が不正です")

    front_path = image_path_for(tenant_id, card_id, "front")
    back_path = image_path_for(tenant_id, card_id, "back")

    if not os.path.exists(front_path) or not os.path.exists(back_path):
        raise HTTPException(
            status_code=400,
            detail="表面と裏面の両方の画像が必要です（片方のみの場合はスワップできません）",
        )

    # 一時ファイル経由で安全にスワップ
    tmp_path = front_path + ".swap.tmp"
    try:
        os.replace(front_path, tmp_path)     # front → tmp
        os.replace(back_path, front_path)    # back → front
        os.replace(tmp_path, back_path)      # tmp(旧front) → back
    except OSError as e:
        # 失敗時は tmp が残っている可能性があるのでクリーンアップ
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"画像の入れ替えに失敗しました: {e}")

    return {"ok": True, "card_id": card_id, "swapped": True}


@app.delete("/api/cards/{card_id}/image")
def delete_card_image(card_id: str, session: dict = Depends(check_session)):
    """画像削除。v6.7: front / 旧 / back の3種をすべて削除"""
    tenant_id = session["tenant_id"]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', card_id):
        raise HTTPException(status_code=400, detail="card_id の形式が不正です")
    deleted_any = False
    for s in ("front", None, "back"):
        path = image_path_for(tenant_id, card_id, s)
        if os.path.exists(path):
            try:
                os.remove(path)
                deleted_any = True
            except OSError:
                pass
    return {"ok": True, "deleted": deleted_any}


# ── 名刺削除 (v6.4) ──
@app.post("/api/cards/delete")
def delete_cards(req: CardDeleteRequest, session: dict = Depends(check_session)):
    """名刺を完全削除（スプシ+SQLite+画像）"""
    tenant_id = session["tenant_id"]
    card_ids = req.card_ids

    if not card_ids:
        raise HTTPException(status_code=400, detail="削除対象が指定されていません")
    if len(card_ids) > 500:
        raise HTTPException(status_code=400, detail="1回の削除は500件までです")

    # 形式チェック
    for cid in card_ids:
        if not re.fullmatch(r'[a-f0-9]{16}', cid):
            raise HTTPException(status_code=400, detail=f"card_id の形式が不正です: {cid}")

    card_id_set = set(card_ids)

    tenant = get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"テナントが見つかりません: {tenant_id}")
    spreadsheet_id = tenant.get("spreadsheet_id")
    sheet_name = tenant.get("sheet_name", "名刺")
    if not spreadsheet_id:
        raise HTTPException(status_code=500, detail="スプレッドシートIDが未設定です")

    # スプシから全行を取得
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A2:K",
    ).execute()
    rows = result.get("values", [])

    # 削除対象以外だけ残す
    kept_rows = []
    removed_count = 0
    for row in rows:
        padded = row + [""] * (11 - len(row))
        company_raw = padded[0] or "不明"
        name_raw = padded[1] or ""
        cid = make_card_id(tenant_id, company_raw, name_raw)
        if cid in card_id_set:
            removed_count += 1
            continue
        kept_rows.append(row)

    if removed_count == 0:
        raise HTTPException(status_code=404, detail="スプレッドシート上に該当する名刺が見つかりません")

    # スプシを書き換え（clear + 残ったものだけupdate）
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A2:Z",
        body={},
    ).execute()
    if kept_rows:
        end_row = len(kept_rows) + 1
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A2:K{end_row}",
            valueInputOption="RAW",
            body={"values": kept_rows},
        ).execute()

    # SQLite の関連データを削除
    db.purge_card_data(tenant_id, list(card_id_set))

    # 画像ファイルを削除 (v6.7: front / 旧 / back の3種)
    image_removed = 0
    for cid in card_id_set:
        for s in ("front", None, "back"):
            path = image_path_for(tenant_id, cid, s)
            if os.path.exists(path):
                try:
                    os.remove(path)
                    image_removed += 1
                except OSError:
                    pass

    # キャッシュクリア
    _cache.pop(tenant_id, None)

    return {
        "ok": True,
        "requested": len(card_ids),
        "sheet_removed": removed_count,
        "images_removed": image_removed,
    }


if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
