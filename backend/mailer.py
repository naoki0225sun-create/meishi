"""
Resend メール送信
==================
差し込み配信機能。
テンプレート内の {{名前}} {{会社}} {{役職}} {{メール}} を置換。
"""
import os
import re
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def render_template(template, card):
    """テンプレート内の {{変数}} を名刺情報で置換"""
    if not template:
        return ""
    t = template
    replacements = {
        "{{名前}}": card.get("name", "") or "",
        "{{会社}}": card.get("company", "") or "",
        "{{役職}}": card.get("department_position", "") or "",
        "{{メール}}": card.get("email", "") or "",
        "{{携帯}}": card.get("mobile", "") or "",
        # 英語版も用意
        "{{name}}": card.get("name", "") or "",
        "{{company}}": card.get("company", "") or "",
        "{{position}}": card.get("department_position", "") or "",
        "{{email}}": card.get("email", "") or "",
    }
    for key, val in replacements.items():
        t = t.replace(key, val)
    return t


def extract_variables(template):
    """テンプレート内で使われている変数を抽出"""
    if not template:
        return []
    return list(set(re.findall(r'\{\{[^}]+\}\}', template)))


def send_email(api_key, from_email, from_name, to_email, subject, body, reply_to=None):
    """
    Resend API でメールを送信する。
    返り値: (ok: bool, provider_id: str | None, error: str | None)
    """
    if not api_key:
        return False, None, "Resend APIキーが設定されていません"
    if not from_email:
        return False, None, "送信元メールアドレスが設定されていません"
    if not to_email:
        return False, None, "宛先メールアドレスが空です"

    from_field = f'"{from_name}" <{from_email}>' if from_name else from_email

    payload = {
        "from": from_field,
        "to": [to_email],
        "subject": subject or "(件名なし)",
        "text": body or "",
    }
    if reply_to:
        payload["reply_to"] = reply_to

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RESEND_API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Cloudflare (Resend前段) の Bot 対策で urllib のデフォルト UA が弾かれるため明示
            "User-Agent": "meishi-app/6.7 (+https://meishi.wako-apps.com)",
            "Accept": "application/json",
        },
        method="POST",
    )

    # デバッグログ
    key_prefix = (api_key[:6] + "...") if api_key else "(empty)"
    logger.warning("[mailer-debug] from=%r to=%r subject=%r key_prefix=%s",
                   from_field, to_email, (subject or "")[:30], key_prefix)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_text = resp.read().decode("utf-8")
            result = json.loads(body_text)
            provider_id = result.get("id")
            logger.warning("[mailer-debug] success id=%r", provider_id)
            return True, provider_id, None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception as re_err:
            err_body = f"(read failed: {re_err})"
        logger.warning("[mailer-debug] HTTPError code=%s body=%s", e.code, err_body)
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message") or err_json.get("error") or err_body
        except Exception:
            msg = err_body or str(e)
        return False, None, f"Resend API エラー ({e.code}): {msg}"
    except Exception as e:
        logger.warning("[mailer-debug] Exception %s: %s", type(e).__name__, e)
        return False, None, f"送信に失敗: {type(e).__name__}: {e}"
