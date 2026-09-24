"""
重複検出ロジック
================
カードリストから重複候補ペアを検出する。
確信度別に分類:
- strong: メール完全一致、電話完全一致
- medium: 会社名+氏名 完全一致（正規化後）
- weak: 氏名完全一致のみ
"""
import re
from collections import defaultdict


def normalize_email(email):
    if not email:
        return ""
    return email.strip().lower()


def normalize_phone(phone):
    if not phone:
        return ""
    # 数字と+以外を削除
    return re.sub(r'[^\d+]', '', phone)


def normalize_name(name):
    if not name:
        return ""
    # 全角スペース・半角スペースを削除、小文字化
    return re.sub(r'\s+', '', name).lower()


def normalize_company_for_compare(company):
    """会社名の比較用正規化"""
    if not company:
        return ""
    c = company.strip()
    c = c.replace("(", "(").replace(")", ")")
    c = re.sub(r'\s*(株式会社|有限会社|合同会社|株\)|㈱)\s*', '', c)
    c = re.sub(r'\s*(Inc\.|Corp\.|Co\.,?\s*Ltd\.?|LLC|Ltd\.?)\s*', '', c, flags=re.IGNORECASE)
    c = re.sub(r'\s+', '', c)
    return c.lower()


def find_duplicates(cards, dismissed_pairs=None):
    """
    カードリストから重複候補ペアを検出して返す。
    返り値: [{"a": card, "b": card, "reason": str, "strength": str}, ...]
    """
    dismissed_pairs = dismissed_pairs or set()

    # インデックス作成
    by_email = defaultdict(list)      # email -> [cards]
    by_phone = defaultdict(list)      # phone -> [cards]
    by_company_name = defaultdict(list)  # (company_norm, name_norm) -> [cards]
    by_name = defaultdict(list)       # name_norm -> [cards]

    for c in cards:
        email = normalize_email(c.get("email"))
        if email:
            by_email[email].append(c)

        for phone_field in ("mobile", "phone"):
            phone = normalize_phone(c.get(phone_field))
            if phone and len(phone) >= 9:  # 短い番号（内線など）は除外
                by_phone[phone].append(c)

        name_n = normalize_name(c.get("name"))
        company_n = normalize_company_for_compare(c.get("company"))
        if name_n:
            by_name[name_n].append(c)
            if company_n:
                by_company_name[(company_n, name_n)].append(c)

    pairs = []
    seen_pairs = set()  # (id_a, id_b) sorted で重複排除

    def add_pair(a, b, reason, strength):
        if a["id"] == b["id"]:
            return
        key = tuple(sorted([a["id"], b["id"]]))
        if key in seen_pairs:
            return
        if key in dismissed_pairs:
            return
        seen_pairs.add(key)
        pairs.append({
            "a": a, "b": b,
            "reason": reason,
            "strength": strength,
        })

    # Strong: メール完全一致
    for email, group in by_email.items():
        if len(group) >= 2:
            for i in range(len(group)):
                for j in range(i+1, len(group)):
                    add_pair(group[i], group[j],
                             f"メールアドレスが一致 ({email})",
                             "strong")

    # Strong: 電話完全一致
    for phone, group in by_phone.items():
        if len(group) >= 2:
            for i in range(len(group)):
                for j in range(i+1, len(group)):
                    add_pair(group[i], group[j],
                             f"電話番号が一致 ({phone})",
                             "strong")

    # Medium: 会社名+氏名 一致
    for (company_n, name_n), group in by_company_name.items():
        if len(group) >= 2:
            for i in range(len(group)):
                for j in range(i+1, len(group)):
                    add_pair(group[i], group[j],
                             "会社名と氏名が一致",
                             "medium")

    # Weak: 氏名のみ一致（同姓同名の可能性）
    for name_n, group in by_name.items():
        if len(group) >= 2 and len(name_n) >= 2:  # 短すぎる名前は除外
            for i in range(len(group)):
                for j in range(i+1, len(group)):
                    add_pair(group[i], group[j],
                             "氏名のみ一致（同姓同名の可能性あり）",
                             "weak")

    # 強さでソート: strong → medium → weak
    strength_order = {"strong": 0, "medium": 1, "weak": 2}
    pairs.sort(key=lambda p: strength_order.get(p["strength"], 99))

    return pairs


def merge_cards(kept_card, removed_card, field_choices):
    """
    field_choices: {"name": "kept" | "removed", ...} 各フィールドで
                   どちらの値を採用するかを指定
    返り値: 新しい値の辞書 {field: value}
    """
    result = {}
    EDITABLE = {"company", "name", "department_position",
                "email", "address", "mobile", "phone", "website"}
    for field in EDITABLE:
        choice = field_choices.get(field, "kept")
        if choice == "removed":
            result[field] = removed_card.get(field, "")
        else:
            result[field] = kept_card.get(field, "")
    return result
