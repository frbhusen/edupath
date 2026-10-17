"""Leaderboard queries and ranking helpers."""

import math
from datetime import datetime, timedelta
from mongoengine.errors import DoesNotExist
from .models import User, StudentGamification, XPEvent, Certificate
from .extensions import cache
from .xp import _calculate_level


def _avatar_text_for_user(user):
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if first or last:
        return (first[:1] + last[:1]).upper()
    username = (getattr(user, "username", "") or "").strip()
    return (username[:2] or "U").upper()


def _certificate_counts_for_students(student_ids):
    if not student_ids:
        return {}

    pipeline = [
        {"$match": {"student_id": {"$in": list(student_ids)}, "is_verified": True}},
        {"$group": {"_id": "$student_id", "count": {"$sum": 1}}},
    ]
    rows = list(Certificate._get_collection().aggregate(pipeline))
    return {row.get("_id"): int(row.get("count", 0) or 0) for row in rows}


def _certificate_count_for_student(student_id):
    if not student_id:
        return 0
    return int(Certificate.objects(student_id=student_id, is_verified=True).count())


def _serialize_leaderboard_entry(profile, user, rank, xp_override=None, student_id_override=None, certificates_count=0):
    display_name = getattr(user, "full_name", None) if user else None
    username = (display_name or getattr(user, "username", "مستخدم")) if user else "مستخدم"
    student_obj_id = None
    if profile and getattr(profile, "student_id", None):
        student_obj_id = profile.student_id.id
    elif student_id_override is not None:
        student_obj_id = student_id_override

    xp_value = int(xp_override if xp_override is not None else (getattr(profile, "xp_total", 0) or 0))
    level_value = int(getattr(profile, "level", _calculate_level(xp_value)) or _calculate_level(xp_value))
    return {
        "rank": rank,
        "username": username,
        "xp": xp_value,
        "level": level_value,
        "badges": list(getattr(profile, "badges", []) or []),
        "avatar": _avatar_text_for_user(user) if user else "U",
        "is_top_3": rank <= 3,
        "student_id": str(student_obj_id) if student_obj_id else None,
        "certificates_count": int(certificates_count or 0),
    }


def _normalize_leaderboard_scope(scope_value):
    allowed = {"all", "weekly", "monthly", "seasonal"}
    scope = (scope_value or "all").strip().lower()
    return scope if scope in allowed else "all"


def _leaderboard_page_cache_key(scope, page, per_page):
    return f"leaderboard_page_{scope}_{page}_{per_page}"


def _scope_start_datetime(scope: str):
    now = datetime.utcnow()
    if scope == "weekly":
        return now - timedelta(days=7)
    if scope == "monthly":
        return now - timedelta(days=30)
    if scope == "seasonal":
        return now - timedelta(days=90)
    return None


def _aggregate_scope_rankings(scope: str, page: int, per_page: int):
    start_dt = _scope_start_datetime(scope)
    if start_dt is None:
        return [], 0

    # استخراج معرّفات أي حساب ليس طالباً (معلمين، مدراء، الخ)
    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]

    events_coll = XPEvent._get_collection()
    match_stage = {"created_at": {"$gte": start_dt}}
    
    # استثناء المعلمين والمدراء من التجميع
    if non_student_ids:
        match_stage["student_id"] = {"$nin": non_student_ids}

    count_pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
        {"$count": "total"},
    ]
    count_result = list(events_coll.aggregate(count_pipeline))
    total_users = int(count_result[0]["total"]) if count_result else 0

    rows_pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
        {"$sort": {"xp_total": -1, "_id": 1}},
        {"$skip": max(0, (page - 1) * per_page)},
        {"$limit": per_page},
    ]
    rows = list(events_coll.aggregate(rows_pipeline))
    return rows, total_users


def _student_scope_xp(student_id, scope: str):
    start_dt = _scope_start_datetime(scope)
    if start_dt is None:
        profile = StudentGamification.objects(student_id=student_id).first()
        return int(profile.xp_total or 0) if profile else 0

    events_coll = XPEvent._get_collection()
    pipeline = [
        {"$match": {"student_id": student_id, "created_at": {"$gte": start_dt}}},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
    ]
    result = list(events_coll.aggregate(pipeline))
    if not result:
        return 0
    return int(result[0].get("xp_total", 0) or 0)


def _leaderboard_payload_for_user(student_id, scope, page, per_page):
    board = _build_leaderboard_page(page=page, per_page=per_page, scope=scope)
    current_rank = None
    current_xp = 0
    current_certificates_count = 0
    if student_id is not None:
        current_rank = _calculate_student_rank(student_id, scope=scope)
        current_xp = _student_scope_xp(student_id=student_id, scope=scope)
        current_certificates_count = _certificate_count_for_student(student_id)
    return {
        "ok": True,
        "leaderboard": board,
        "current_rank": current_rank,
        "current_xp": current_xp,
        "current_certificates_count": current_certificates_count,
    }


def _build_leaderboard_page(page: int, per_page: int, scope: str = "all"):
    scope = _normalize_leaderboard_scope(scope)
    page = max(1, int(page or 1))
    per_page = max(10, min(int(per_page or 20), 50))
    cache_key = _leaderboard_page_cache_key(scope, page, per_page)
    cached = cache.get(cache_key)
    if cached:
        return cached

    # استخراج معرّفات الموظفين لاستثنائهم
    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]

    if scope == "all":
        total_users = StudentGamification.objects(student_id__nin=non_student_ids).count()
        total_pages = max(1, math.ceil(total_users / per_page)) if total_users else 1
        if page > total_pages:
            page = total_pages

        start_rank = ((page - 1) * per_page) + 1
        profiles = list(
            StudentGamification.objects(student_id__nin=non_student_ids)
            .order_by("-xp_total", "student_id")
            .skip((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        student_ids = []
        valid_profiles = []
        for p in profiles:
            try:
                if p.student_id:
                    student_ids.append(p.student_id.id)
                    valid_profiles.append(p)
            except DoesNotExist:
                pass

        users = User.objects(id__in=student_ids).only("id", "username", "first_name", "last_name").all() if student_ids else []
        users_by_id = {u.id: u for u in users}
        cert_counts = _certificate_counts_for_students(student_ids)

        entries = []
        for i, profile in enumerate(valid_profiles):
            try:
                user = users_by_id.get(profile.student_id.id) if profile.student_id else None
                sid = profile.student_id.id if profile.student_id else None
            except DoesNotExist:
                continue
            entries.append(
                _serialize_leaderboard_entry(
                    profile,
                    user,
                    start_rank + len(entries),
                    certificates_count=cert_counts.get(sid, 0),
                )
            )
    else:
        rows, total_users = _aggregate_scope_rankings(scope=scope, page=page, per_page=per_page)
        total_pages = max(1, math.ceil(total_users / per_page)) if total_users else 1
        if page > total_pages:
            page = total_pages
            rows, total_users = _aggregate_scope_rankings(scope=scope, page=page, per_page=per_page)

        start_rank = ((page - 1) * per_page) + 1
        student_ids = [row.get("_id") for row in rows if row.get("_id")]
        users = User.objects(id__in=student_ids).only("id", "username", "first_name", "last_name").all() if student_ids else []
        users_by_id = {u.id: u for u in users}
        
        profiles = StudentGamification.objects(student_id__in=student_ids).only("student_id", "level", "badges").all() if student_ids else []
        profiles_by_student_id = {}
        for p in profiles:
            try:
                if p.student_id:
                    profiles_by_student_id[p.student_id.id] = p
            except DoesNotExist:
                pass

        cert_counts = _certificate_counts_for_students(student_ids)

        entries = []
        for i, row in enumerate(rows):
            sid = row.get("_id")
            xp_value = int(row.get("xp_total", 0) or 0)
            user = users_by_id.get(sid)
            profile = profiles_by_student_id.get(sid)
            entries.append(
                _serialize_leaderboard_entry(
                    profile=profile,
                    user=user,
                    rank=start_rank + len(entries),
                    xp_override=xp_value,
                    student_id_override=sid,
                    certificates_count=cert_counts.get(sid, 0),
                )
            )

    payload = {
        "entries": entries,
        "page": page,
        "per_page": per_page,
        "scope": scope,
        "total_users": total_users,
        "total_pages": total_pages,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    cache.set(cache_key, payload, timeout=15)
    return payload


def _calculate_student_rank(student_id, scope: str = "all"):
    if not student_id:
        return None
    scope = _normalize_leaderboard_scope(scope)

    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]

    if scope == "all":
        profile = StudentGamification.objects(student_id=student_id).first()
        if not profile:
            return None

        higher_count = StudentGamification.objects(
            student_id__nin=non_student_ids,
            __raw__={
                "$or": [
                    {"xp_total": {"$gt": int(profile.xp_total or 0)}},
                    {
                        "xp_total": int(profile.xp_total or 0),
                        "student_id": {"$lt": profile.student_id.id},
                    },
                ]
            }
        ).count()
        return higher_count + 1

    current_xp = _student_scope_xp(student_id=student_id, scope=scope)
    if current_xp <= 0:
        return None

    start_dt = _scope_start_datetime(scope)
    events_coll = XPEvent._get_collection()
    match_stage = {"created_at": {"$gte": start_dt}}
    if non_student_ids:
        match_stage["student_id"] = {"$nin": non_student_ids}

    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
        {
            "$match": {
                "$or": [
                    {"xp_total": {"$gt": current_xp}},
                    {
                        "$and": [
                            {"xp_total": current_xp},
                            {"_id": {"$lt": student_id}},
                        ]
                    },
                ]
            }
        },
        {"$count": "higher"},
    ]
    higher_result = list(events_coll.aggregate(pipeline))
    higher_count = int(higher_result[0]["higher"]) if higher_result else 0
    return higher_count + 1
