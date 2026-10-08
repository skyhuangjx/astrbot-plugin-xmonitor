from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
MESSAGE_TYPES = {"GroupMessage", "FriendMessage"}
MAX_ACCOUNTS = 100
MAX_TARGETS = 100
MAX_BINDINGS = 1000


def empty_graph() -> dict[str, Any]:
    return {"accounts": [], "targets": [], "bindings": [], "cursors": {}}


class MonitorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_graph()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty_graph()
        try:
            graph = normalize_graph(raw)
        except ValueError:
            return empty_graph()
        cursors = raw.get("cursors", {}) if isinstance(raw, dict) else {}
        if isinstance(cursors, dict):
            graph["cursors"] = {
                str(key): str(value)
                for key, value in cursors.items()
                if _is_post_id(value)
            }
        return graph

    def save(self, graph: dict[str, Any]) -> None:
        normalized = normalize_graph(graph)
        normalized["cursors"] = {
            str(key): str(value)
            for key, value in graph.get("cursors", {}).items()
            if _is_post_id(value)
        }
        fd, temp_name = tempfile.mkstemp(
            prefix="monitor-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def normalize_graph(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("graph must be an object")
    accounts = _normalize_accounts(raw.get("accounts", []))
    targets = _normalize_targets(raw.get("targets", []))
    account_ids = {item["id"] for item in accounts}
    target_ids = {item["id"] for item in targets}
    bindings = _normalize_bindings(raw.get("bindings", []), account_ids, target_ids)
    return {
        "accounts": accounts,
        "targets": targets,
        "bindings": bindings,
        "cursors": dict(raw.get("cursors", {}))
        if isinstance(raw.get("cursors", {}), dict)
        else {},
    }


def _normalize_accounts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_ACCOUNTS:
        raise ValueError("accounts must be a list")
    result: list[dict[str, Any]] = []
    seen_usernames: set[str] = set()
    seen_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid account")
        username = str(item.get("username", "")).strip().lstrip("@").lower()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError(f"invalid username: {username}")
        if username in seen_usernames:
            continue
        account_id = _safe_id(item.get("id"), "account")
        while account_id in seen_ids:
            account_id = _new_id("account")
        seen_usernames.add(username)
        seen_ids.add(account_id)
        label = _safe_label(item.get("label"), username)
        result.append(
            {
                "id": account_id,
                "username": username,
                "label": label,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return result


def _normalize_targets(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_TARGETS:
        raise ValueError("targets must be a list")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid target")
        platform_id = str(item.get("platform_id", "")).strip()
        message_type = str(item.get("message_type", "")).strip()
        session_id = str(item.get("session_id", "")).strip()
        if not platform_id or len(platform_id) > 120:
            raise ValueError("invalid platform_id")
        if message_type not in MESSAGE_TYPES:
            raise ValueError("invalid message_type")
        if not session_id or len(session_id) > 300:
            raise ValueError("invalid session_id")
        target_key = (platform_id, message_type, session_id)
        if target_key in seen_keys:
            continue
        target_id = _safe_id(item.get("id"), "target")
        while target_id in seen_ids:
            target_id = _new_id("target")
        seen_ids.add(target_id)
        seen_keys.add(target_key)
        result.append(
            {
                "id": target_id,
                "label": _safe_label(item.get("label"), session_id),
                "platform_id": platform_id,
                "message_type": message_type,
                "session_id": session_id,
            }
        )
    return result


def _normalize_bindings(
    raw: Any, account_ids: set[str], target_ids: set[str]
) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > MAX_BINDINGS:
        raise ValueError("bindings must be a list")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid binding")
        account_id = str(item.get("account_id", ""))
        target_id = str(item.get("target_id", ""))
        pair = (account_id, target_id)
        if account_id not in account_ids or target_id not in target_ids:
            raise ValueError("binding references unknown node")
        if pair in seen:
            continue
        seen.add(pair)
        result.append({"account_id": account_id, "target_id": target_id})
    return result


def edge_key(account_id: str, target_id: str) -> str:
    return f"{account_id}:{target_id}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _safe_id(value: Any, prefix: str) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", raw):
        return raw
    return _new_id(prefix)


def _safe_label(value: Any, fallback: str) -> str:
    label = str(value or fallback).strip()
    return label[:80] or fallback[:80]


def _is_post_id(value: Any) -> bool:
    return bool(re.fullmatch(r"\d+", str(value)))
