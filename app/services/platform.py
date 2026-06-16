import hashlib
import os
import re
import sqlite3
import string
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_SUBROUTER_BASE_URL = "http://subrouter.railway.internal:8080"
DEFAULT_PUBLIC_SUBROUTER_BASE_URL = "https://api.subrouter.com"
AUTO_KEY_PREFIX = "moneyprinterturbo-auto"

current_user_id: ContextVar[str] = ContextVar("mpt_current_user_id", default="")
current_llm_credentials: ContextVar[dict[str, str] | None] = ContextVar(
    "mpt_current_llm_credentials", default=None
)


class PlatformError(Exception):
    pass


def platform_enabled() -> bool:
    value = os.environ.get("MPT_PLATFORM_ENABLED") or os.environ.get("WEBNOVEL_PLATFORM_ENABLED")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("MPT_DATA_DIR"))


def platform_data_dir() -> Path:
    configured = os.environ.get("MPT_DATA_DIR") or os.environ.get("WEBNOVEL_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return Path("/data")
    return (Path.home() / ".moneyprinterturbo-platform").resolve()


def user_data_dir(user_id: str) -> Path:
    safe_user = _slugify(user_id, "user")
    return platform_data_dir() / "users" / safe_user


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str, fallback: str) -> str:
    allowed = string.ascii_letters + string.digits + "-"
    text = re.sub(r"\s+", "-", str(value or "").strip().lower())
    text = "".join(ch if ch in allowed else "-" for ch in text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def _normalize_base_url(value: str | None) -> str:
    raw = (value or default_subrouter_base_url()).strip().rstrip("/")
    if not raw:
        return DEFAULT_SUBROUTER_BASE_URL
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw


def _gateway_base_url(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def default_subrouter_base_url() -> str:
    return (
        os.environ.get("SUBROUTER_BASE_URL")
        or os.environ.get("SUBROUTERAI_BASE_URL")
        or os.environ.get("TOONFLOW_SUBROUTER_BASE_URL")
        or DEFAULT_SUBROUTER_BASE_URL
    )


def _parse_candidates(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,\n;]", value) if part.strip()]


def default_login_base_url_candidates() -> list[str]:
    candidates = [
        os.environ.get("SUBROUTER_BASE_URL"),
        os.environ.get("SUBROUTERAI_BASE_URL"),
        os.environ.get("TOONFLOW_SUBROUTER_BASE_URL"),
        *_parse_candidates(os.environ.get("SUBROUTER_BASE_URL_CANDIDATES")),
        *_parse_candidates(os.environ.get("TOONFLOW_SUBROUTER_BASE_URL_CANDIDATES")),
        DEFAULT_SUBROUTER_BASE_URL,
        DEFAULT_PUBLIC_SUBROUTER_BASE_URL,
    ]
    result = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = _normalize_base_url(candidate)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _normalize_api_key(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        return ""
    return f"sk-{key.removeprefix('sk-')}"


def _bearer(api_key: str) -> str:
    return f"Bearer {api_key.removeprefix('Bearer ').removeprefix('bearer ')}"


def _extract_items(payload: Any) -> list[Any]:
    candidates = []
    if isinstance(payload, dict):
        data = payload.get("data")
        candidates.extend(
            [
                data.get("items") if isinstance(data, dict) else None,
                data.get("models") if isinstance(data, dict) else None,
                data.get("data") if isinstance(data, dict) else None,
                data,
                payload.get("items"),
                payload.get("models"),
            ]
        )
    candidates.append(payload)
    for item in candidates:
        if isinstance(item, list):
            return item
    return []


def _extract_user(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("user")
        if isinstance(nested, dict):
            return nested
        return data
    user = payload.get("user")
    return user if isinstance(user, dict) else {}


def _extract_distributor(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw = body.get("distributor") if isinstance(body.get("distributor"), dict) else {}
    dist_id = body.get("distributor_id") or body.get("distributorId") or raw.get("id")
    belongs = body.get("belongs_to_distributor")
    if belongs is None:
        belongs = body.get("belongsToDistributor")
    if belongs is None:
        belongs = bool(dist_id)
    if not belongs:
        return None
    slug = str(raw.get("slug") or body.get("distributor_slug") or body.get("distributorSlug") or "").strip()
    if not dist_id or not slug:
        raise PlatformError("用户属于分站，但 SubRouter 未返回分站 slug")
    return {
        "id": str(dist_id),
        "slug": slug,
        "name": str(raw.get("name") or body.get("distributor_name") or body.get("distributorName") or ""),
    }


def _extract_key(payload: Any) -> tuple[str, str]:
    body = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(body, dict):
        nested = (
            body.get("token")
            or body.get("key_info")
            or body.get("keyInfo")
            or body.get("apiKey")
            or body.get("api_key")
        )
        if isinstance(nested, dict):
            key = str(nested.get("key") or nested.get("api_key") or nested.get("apiKey") or nested.get("token") or "")
            return _normalize_api_key(key), str(nested.get("id") or "")
        key = str(body.get("key") or body.get("api_key") or body.get("apiKey") or body.get("token") or "")
        return _normalize_api_key(key), str(body.get("id") or "")
    return "", ""


def _find_reusable_key(items: list[Any], exact_name: str | None = None) -> tuple[str, str] | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if exact_name is not None and name != exact_name:
            continue
        if exact_name is None and not name.startswith(AUTO_KEY_PREFIX):
            continue
        key = _normalize_api_key(str(item.get("key") or item.get("api_key") or item.get("apiKey") or item.get("token") or ""))
        if key:
            return key, str(item.get("id") or "")
    return None


def _infer_model_type(model_id: str) -> str:
    text = model_id.lower()
    if re.search(r"video|seedance|wan|kling|veo|sora|runway|hailuo|luma|pixverse", text):
        return "video"
    if re.search(r"image|img|seedream|nano|gpt-image|flux|dalle|dall-e|midjourney|ideogram", text):
        return "image"
    return "text"


def _pick_default_model(models: list[dict[str, Any]]) -> str:
    text_models = [item["id"] for item in models if item.get("type") == "text" and item.get("id")]
    if not text_models:
        return models[0]["id"] if models else ""
    preferences = [
        r"claude.*sonnet|sonnet",
        r"gpt-5|gpt-4\.?1|gpt-4o|gpt-4|o3|o4",
        r"deepseek.*(v3|chat|pro)",
        r"qwen.*(max|plus|72b|32b|coder)|qwen3",
        r"glm.*(5|4\.5|4-5)|kimi|moonshot",
    ]
    for pattern in preferences:
        for model_id in text_models:
            if re.search(pattern, model_id, re.I):
                return model_id
    return text_models[0]


def _upstream_error(response: requests.Response, fallback: str) -> str:
    text = response.text[:1000]
    try:
        payload = response.json()
    except ValueError:
        return text or fallback
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or fallback)
        if isinstance(error, str):
            return error
        if payload.get("message"):
            return str(payload["message"])
    return text or fallback


class PlatformStore:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or platform_data_dir()
        self.db_path = self.data_dir / "platform.sqlite3"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL DEFAULT '',
                    subrouter_provider TEXT NOT NULL DEFAULT 'subrouterai',
                    subrouter_api_key TEXT NOT NULL DEFAULT '',
                    subrouter_base_url TEXT NOT NULL DEFAULT 'http://subrouter.railway.internal:8080',
                    subrouter_external_user_id TEXT NOT NULL DEFAULT '',
                    subrouter_session_cookie TEXT NOT NULL DEFAULT '',
                    subrouter_api_key_id TEXT NOT NULL DEFAULT '',
                    subrouter_distributor_id TEXT NOT NULL DEFAULT '',
                    subrouter_distributor_slug TEXT NOT NULL DEFAULT '',
                    subrouter_distributor_name TEXT NOT NULL DEFAULT '',
                    default_model TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def health(self) -> dict[str, Any]:
        return {"ok": True, "platform": platform_enabled(), "data_dir": str(self.data_dir)}

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise PlatformError("登录已过期，请重新登录")
        return self._public_user(dict(row))

    def _public_user(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "email": row.get("email") or "",
            "subrouter": {
                "configured": bool(str(row.get("subrouter_api_key") or "").strip()),
                "default_model": row.get("default_model") or "",
                "distributor_id": row.get("subrouter_distributor_id") or "",
                "distributor_slug": row.get("subrouter_distributor_slug") or "",
                "distributor_name": row.get("subrouter_distributor_name") or "",
                "account_type": "dist" if row.get("subrouter_distributor_id") else "main",
            },
        }

    def subrouter_password_login(self, *, username: str, password: str) -> dict[str, Any]:
        username = username.strip()
        password = password.strip()
        if not username or not password:
            raise PlatformError("SubRouter 用户名和密码不能为空")

        last_error = "SubRouter 登录失败"
        for candidate in default_login_base_url_candidates():
            try:
                with requests.Session() as session:
                    login = self._login_subrouterai(session, candidate, username, password)
                    return self._prepare_subrouterai_account(session, login, fallback_username=username)
            except Exception as exc:
                last_error = str(exc)
        raise PlatformError(last_error)

    def _login_subrouterai(
        self,
        session: requests.Session,
        base_url: str,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        response = session.post(
            f"{_normalize_base_url(base_url)}/api/user/login",
            json={"username": username, "password": password},
            timeout=20,
            allow_redirects=False,
        )
        if response.status_code >= 400:
            raise PlatformError(_upstream_error(response, "SubRouter 登录失败"))
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise PlatformError(str(payload.get("message") or "SubRouter 用户名或密码错误"))
        cookie = "; ".join(f"{item.name}={item.value}" for item in session.cookies)
        if not cookie:
            raise PlatformError("SubRouter 登录成功但未返回会话 Cookie")
        user = _extract_user(payload)
        distributor = _extract_distributor(payload)
        return {
            "provider": "subrouterai",
            "base_url": _normalize_base_url(base_url),
            "external_user_id": str(user.get("id") or "").strip(),
            "username": str(user.get("username") or username),
            "email": str(user.get("email") or ""),
            "display_name": str(user.get("display_name") or user.get("displayName") or user.get("username") or username),
            "session_cookie": cookie,
            "distributor": distributor,
        }

    def _prepare_subrouterai_account(
        self,
        session: requests.Session,
        login: dict[str, Any],
        *,
        fallback_username: str,
    ) -> dict[str, Any]:
        account_seed = login.get("external_user_id") or login.get("email") or login.get("username") or fallback_username
        user_id = "sr_" + hashlib.sha256(f"subrouterai:{account_seed}".encode("utf-8")).hexdigest()[:24]
        api_key, api_key_id = self._ensure_subrouterai_key(session, login)
        models = self._fetch_gateway_models(api_key, login["base_url"])
        default_model = _pick_default_model(models)
        now = _utc_now()
        dist = login.get("distributor") or {}
        username = _slugify(f"subrouter-{login.get('username') or fallback_username}", f"subrouter-{user_id[-8:]}")
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?, email = ?, subrouter_api_key = ?,
                        subrouter_base_url = ?, subrouter_external_user_id = ?,
                        subrouter_session_cookie = ?, subrouter_api_key_id = ?,
                        subrouter_distributor_id = ?, subrouter_distributor_slug = ?,
                        subrouter_distributor_name = ?,
                        default_model = COALESCE(NULLIF(default_model, ''), ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        login.get("email") or "",
                        api_key,
                        login["base_url"],
                        login.get("external_user_id") or "",
                        login.get("session_cookie") or "",
                        api_key_id,
                        dist.get("id") or "",
                        dist.get("slug") or "",
                        dist.get("name") or "",
                        default_model,
                        now,
                        user_id,
                    ),
                )
            else:
                candidate = username
                suffix = 0
                while conn.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (candidate, user_id)).fetchone():
                    suffix += 1
                    candidate = f"{username}-{suffix}"
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, email, subrouter_api_key, subrouter_base_url,
                        subrouter_external_user_id, subrouter_session_cookie,
                        subrouter_api_key_id, subrouter_distributor_id,
                        subrouter_distributor_slug, subrouter_distributor_name,
                        default_model, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        candidate,
                        login.get("email") or "",
                        api_key,
                        login["base_url"],
                        login.get("external_user_id") or "",
                        login.get("session_cookie") or "",
                        api_key_id,
                        dist.get("id") or "",
                        dist.get("slug") or "",
                        dist.get("name") or "",
                        default_model,
                        now,
                        now,
                    ),
                )
        return self.get_user(user_id)

    def _auth_headers(self, login: dict[str, Any]) -> dict[str, str]:
        headers = {"Cookie": str(login.get("session_cookie") or "")}
        if login.get("external_user_id"):
            headers["New-Api-User"] = str(login["external_user_id"])
        return headers

    def _auth_headers_from_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
        headers = {"Cookie": str(row["subrouter_session_cookie"] or "")}
        external_user_id = str(row["subrouter_external_user_id"] or "")
        if external_user_id:
            headers["New-Api-User"] = external_user_id
        return headers

    def _list_subrouterai_keys(self, session: requests.Session, login: dict[str, Any]) -> list[Any]:
        headers = self._auth_headers(login)
        if login.get("distributor"):
            response = session.get(
                f"{login['base_url']}/api/user/self/distributor/token/list",
                headers=headers,
                params={"page": 1, "page_size": 100},
                timeout=20,
            )
        else:
            response = session.get(f"{login['base_url']}/api/token/", headers=headers, timeout=20)
        if response.status_code >= 400:
            raise PlatformError(_upstream_error(response, "获取 SubRouter 密钥列表失败"))
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise PlatformError(str(payload.get("message") or "获取 SubRouter 密钥列表失败"))
        return _extract_items(payload)

    def _ensure_subrouterai_key(self, session: requests.Session, login: dict[str, Any]) -> tuple[str, str]:
        existing = _find_reusable_key(self._list_subrouterai_keys(session, login))
        if existing:
            return existing
        headers = self._auth_headers(login)
        name = f"{AUTO_KEY_PREFIX}-{int(time.time())}"
        if login.get("distributor"):
            path = "/api/user/self/distributor/token/create"
            body = {"name": name, "key_group_id": 0}
        else:
            path = "/api/token/"
            body = {
                "name": name,
                "group": "subrouter",
                "expired_time": -1,
                "remain_quota": 0,
                "unlimited_quota": True,
                "model_limits_enabled": False,
            }
        response = session.post(f"{login['base_url']}{path}", headers=headers, json=body, timeout=20)
        if response.status_code >= 400:
            raise PlatformError(_upstream_error(response, "创建 SubRouter 访问密钥失败"))
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise PlatformError(str(payload.get("message") or "创建 SubRouter 访问密钥失败"))
        key = _extract_key(payload)
        if key[0]:
            return key
        created = _find_reusable_key(self._list_subrouterai_keys(session, login), exact_name=name)
        if not created:
            raise PlatformError("SubRouter 密钥已创建但未能从列表读取")
        return created

    def _fetch_gateway_models(self, api_key: str, base_url: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{_gateway_base_url(base_url)}/models",
            headers={"Authorization": _bearer(api_key)},
            timeout=30,
        )
        if response.status_code >= 400:
            raise PlatformError(_upstream_error(response, "读取模型列表失败"))
        payload = response.json()
        models = []
        for item in _extract_items(payload):
            if isinstance(item, dict):
                model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
                category = str(item.get("category") or item.get("type") or "")
            else:
                model_id = str(item or "").strip()
                category = ""
            if model_id:
                models.append({"id": model_id, "type": _infer_model_type(f"{model_id} {category}")})
        models.sort(key=lambda item: item["id"])
        return models

    def _fetch_subscribed_models(self, row: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
        response = requests.get(
            f"{_normalize_base_url(row['subrouter_base_url'])}/api/user/self/subrouter/models",
            headers=self._auth_headers_from_row(row),
            timeout=20,
        )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise PlatformError(_upstream_error(response, "读取 SubRouter 订阅模型失败"))
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise PlatformError(str(payload.get("message") or "读取 SubRouter 订阅模型失败"))
        models = []
        for item in _extract_items(payload):
            if not isinstance(item, dict):
                continue
            model_id = str(
                item.get("model_name")
                or item.get("modelName")
                or item.get("id")
                or item.get("name")
                or ""
            ).strip()
            category = str(item.get("category") or item.get("type") or "")
            if model_id:
                models.append({"id": model_id, "type": _infer_model_type(f"{model_id} {category}")})
        models.sort(key=lambda item: item["id"])
        return models

    def fetch_models(self, user_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise PlatformError("登录已过期，请重新登录")
        api_key = str(row["subrouter_api_key"] or "").strip()
        if not api_key:
            raise PlatformError("当前账号未准备好模型调用密钥，请重新登录")
        default_model = str(row["default_model"] or "")
        models = []
        if not row["subrouter_distributor_id"]:
            models = self._fetch_subscribed_models(row)
        if not models:
            models = self._fetch_gateway_models(api_key, row["subrouter_base_url"])
        model_ids = {item["id"] for item in models if item.get("id")}
        if not default_model or default_model not in model_ids:
            default_model = _pick_default_model(models)
            if default_model:
                self.set_default_model(user_id, default_model)
        return {"models": models, "default_model": default_model}

    def set_default_model(self, user_id: str, model: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET default_model = ?, updated_at = ? WHERE id = ?",
                (model.strip(), _utc_now(), user_id),
            )

    def subrouter_credentials(self, user_id: str) -> tuple[str, str, str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT subrouter_api_key, subrouter_base_url, default_model FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            raise PlatformError("登录已过期，请重新登录")
        api_key = str(row["subrouter_api_key"] or "").strip()
        if not api_key:
            raise PlatformError("当前账号未准备好模型调用密钥，请重新登录")
        return api_key, _gateway_base_url(row["subrouter_base_url"]), str(row["default_model"] or "")


_store: PlatformStore | None = None


def get_store() -> PlatformStore:
    global _store
    if _store is None:
        _store = PlatformStore()
    return _store


def activate_user_context(user_id: str) -> None:
    current_user_id.set(user_id)
    api_key, base_url, model = get_store().subrouter_credentials(user_id)
    current_llm_credentials.set({"api_key": api_key, "base_url": base_url, "model": model})
