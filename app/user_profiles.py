from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

from .config import USERS_FILE, CURRENT_USER_ID_FILE, USER_FILE


LEGACY_PROFILES: dict[str, dict[str, Any]] = {
    "JD": {
        "initials": "JD",
        "first_name": "Jean",
        "last_name": "Dard",
        "date_of_birth": "1997-09-02",
        "gender": "Homme",
        "active": True,
    },
    "LL": {
        "initials": "LL",
        "first_name": "Loïse",
        "last_name": "Lyonnet",
        "date_of_birth": "1997-08-04",
        "gender": "Femme",
        "active": True,
    },
}


def stable_user_id(initials: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cycle-analyst:user:{initials.upper()}"))


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _profile_from_legacy(initials: str) -> dict[str, Any]:
    initials = (initials or "").strip().upper()
    base = dict(LEGACY_PROFILES.get(initials, {}))
    if not base:
        base = {
            "initials": initials or "??",
            "first_name": "",
            "last_name": "",
            "date_of_birth": "",
            "gender": "",
            "active": True,
        }
    base["user_id"] = stable_user_id(base["initials"])
    base.setdefault("created_at", _now())
    base.setdefault("updated_at", base["created_at"])
    base.setdefault("sync_status", "pending")
    return normalize_profile(base)


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    initials = str(profile.get("initials") or "").strip().upper()
    first_name = str(profile.get("first_name") or "").strip()
    last_name = str(profile.get("last_name") or "").strip()
    user_id = str(profile.get("user_id") or (stable_user_id(initials) if initials else uuid.uuid4())).strip()
    normalized = {
        "user_id": user_id,
        "initials": initials or _initials_from_name(first_name, last_name),
        "first_name": first_name,
        "last_name": last_name,
        "date_of_birth": str(profile.get("date_of_birth") or "").strip(),
        "gender": str(profile.get("gender") or "").strip(),
        "active": bool(profile.get("active", True)),
        "created_at": str(profile.get("created_at") or _now()),
        "updated_at": str(profile.get("updated_at") or _now()),
        "sync_status": str(profile.get("sync_status") or "pending"),
    }
    return normalized


def _initials_from_name(first_name: str, last_name: str) -> str:
    letters = [part[0] for part in (first_name, last_name) if part]
    return "".join(letters).upper() or "??"


def _read_profiles_file() -> list[dict[str, Any]] | None:
    if not os.path.exists(USERS_FILE):
        return None
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        profiles = data.get("users", [])
    else:
        profiles = data
    if not isinstance(profiles, list):
        return []
    return [normalize_profile(p) for p in profiles if isinstance(p, dict)]


def save_profiles(profiles: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    payload = {
        "version": 1,
        "users": [normalize_profile(p) for p in profiles],
    }
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_profiles() -> list[dict[str, Any]]:
    profiles = _read_profiles_file()
    if profiles is None:
        return []
    return profiles


def active_profiles() -> list[dict[str, Any]]:
    return [p for p in load_profiles() if p.get("active", True)]


def legacy_profiles_for_initials(initials_values: set[str]) -> list[dict[str, Any]]:
    profiles = []
    for initials in sorted({(v or "").strip().upper() for v in initials_values if (v or "").strip()}):
        profiles.append(_profile_from_legacy(initials))
    return profiles


def ensure_profiles_for_legacy_initials(initials_values: set[str]) -> list[dict[str, Any]]:
    profiles = load_profiles()
    existing_initials = {p["initials"] for p in profiles}
    changed = False
    for profile in legacy_profiles_for_initials(initials_values):
        if profile["initials"] not in existing_initials:
            profiles.append(profile)
            existing_initials.add(profile["initials"])
            changed = True
    if changed:
        save_profiles(profiles)
    return profiles


def get_profile(user_id_or_initials: str | None) -> dict[str, Any] | None:
    key = (user_id_or_initials or "").strip()
    if not key:
        return None
    for profile in load_profiles():
        if profile["user_id"] == key or profile["initials"] == key.upper():
            return profile
    if key.upper() in LEGACY_PROFILES:
        return _profile_from_legacy(key)
    return None


def create_profile(
    *,
    initials: str,
    first_name: str,
    last_name: str,
    date_of_birth: str = "",
    gender: str = "",
    active: bool = True,
) -> dict[str, Any]:
    profile = normalize_profile(
        {
            "user_id": str(uuid.uuid4()),
            "initials": initials,
            "first_name": first_name,
            "last_name": last_name,
            "date_of_birth": date_of_birth,
            "gender": gender,
            "active": active,
            "created_at": _now(),
            "updated_at": _now(),
            "sync_status": "pending",
        }
    )
    profiles = load_profiles()
    profiles.append(profile)
    save_profiles(profiles)
    return profile


def upsert_profile(profile: dict[str, Any], *, active: bool | None = None) -> dict[str, Any]:
    incoming = normalize_profile(profile)
    if active is not None:
        incoming["active"] = active
    profiles = load_profiles()
    for index, existing in enumerate(profiles):
        if existing["user_id"] == incoming["user_id"]:
            merged = existing | incoming
            merged["updated_at"] = _now()
            profiles[index] = normalize_profile(merged)
            save_profiles(profiles)
            return profiles[index]
    profiles.append(incoming)
    save_profiles(profiles)
    return incoming


def hide_profile(user_id: str) -> bool:
    profiles = load_profiles()
    changed = False
    for profile in profiles:
        if profile["user_id"] == user_id:
            profile["active"] = False
            profile["updated_at"] = _now()
            profile["sync_status"] = "pending"
            changed = True
    if changed:
        save_profiles(profiles)
    return changed


def save_current_user_id(user_id: str) -> None:
    os.makedirs(os.path.dirname(CURRENT_USER_ID_FILE), exist_ok=True)
    with open(CURRENT_USER_ID_FILE, "w", encoding="utf-8") as f:
        f.write(user_id)


def load_current_user_id() -> str | None:
    if os.path.exists(CURRENT_USER_ID_FILE):
        try:
            with open(CURRENT_USER_ID_FILE, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception:
            return None
    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, "r", encoding="utf-8") as f:
                initials = f.read().strip().upper()
        except Exception:
            initials = ""
        profile = get_profile(initials)
        if profile:
            save_current_user_id(profile["user_id"])
            return profile["user_id"]
    return None


def profile_snapshot(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not profile:
        return {}
    return {
        "user_id": profile.get("user_id"),
        "initials": profile.get("initials"),
        "first_name": profile.get("first_name"),
        "last_name": profile.get("last_name"),
        "date_of_birth": profile.get("date_of_birth"),
        "gender": profile.get("gender"),
    }


def profile_snapshot_json(profile: dict[str, Any] | None) -> str | None:
    snapshot = profile_snapshot(profile)
    if not snapshot:
        return None
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
