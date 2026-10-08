from __future__ import annotations

import copy
import html
import re
from collections.abc import Mapping
from typing import Any

from .models import Tweet, TweetMedia, TweetReference, XUser

RAW_URL_KEEP_MAX_LENGTH = 60


def merge_api_payloads(base: Mapping[str, Any], *extras: Mapping[str, Any]) -> dict[str, Any]:
    """Merge X API response payloads while preserving the first top-level data block."""
    merged = copy.deepcopy(dict(base))
    includes = merged.setdefault("includes", {})

    for extra in extras:
        extra_includes = extra.get("includes", {})
        _merge_include_list(includes, "users", extra_includes.get("users", []), "id")
        _merge_include_list(includes, "tweets", extra_includes.get("tweets", []), "id")
        _merge_include_list(includes, "media", extra_includes.get("media", []), "media_key")

        extra_data = _as_list(extra.get("data"))
        _merge_include_list(includes, "tweets", extra_data, "id")

    return merged


def collect_referenced_tweet_ids(payload: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    for tweet in _all_raw_tweets(payload):
        for reference in tweet.get("referenced_tweets", []) or []:
            tweet_id = str(reference.get("id") or "")
            if tweet_id and tweet_id not in seen:
                seen.add(tweet_id)
                ids.append(tweet_id)

    return ids


def parse_tweet_response(payload: Mapping[str, Any]) -> list[Tweet]:
    includes = payload.get("includes", {})
    users = {
        str(user["id"]): _parse_user(user)
        for user in includes.get("users", []) or []
        if user.get("id") is not None
    }
    media = {
        str(item["media_key"]): _parse_media(item)
        for item in includes.get("media", []) or []
        if item.get("media_key") is not None
    }

    raw_tweets: dict[str, Mapping[str, Any]] = {}
    for tweet in _all_raw_tweets(payload):
        tweet_id = tweet.get("id")
        if tweet_id is not None:
            raw_tweets[str(tweet_id)] = tweet

    building: set[str] = set()
    built: dict[str, Tweet] = {}

    def build(tweet_id: str) -> Tweet:
        if tweet_id in built:
            return built[tweet_id]
        if tweet_id in building:
            return _unknown_tweet(tweet_id)

        raw = raw_tweets.get(tweet_id)
        if raw is None:
            return _unknown_tweet(tweet_id)

        building.add(tweet_id)
        author_id = str(raw.get("author_id") or "")
        author = users.get(author_id) or XUser(
            id=author_id or "unknown",
            name="Unknown",
            username="unknown",
        )
        media_items = [
            media[key]
            for key in raw.get("attachments", {}).get("media_keys", []) or []
            if key in media
        ]
        references = [
            TweetReference(
                type=str(reference.get("type") or "unknown"),
                tweet=build(str(reference.get("id"))),
            )
            for reference in raw.get("referenced_tweets", []) or []
            if reference.get("id") is not None
        ]
        tweet = Tweet(
            id=tweet_id,
            text=clean_tweet_text(raw),
            author=author,
            created_at=raw.get("created_at"),
            media=media_items,
            references=references,
            raw=dict(raw),
        )
        built[tweet_id] = tweet
        building.remove(tweet_id)
        return tweet

    return [
        build(str(tweet["id"]))
        for tweet in _as_list(payload.get("data"))
        if tweet.get("id") is not None
    ]


def clean_tweet_text(raw_tweet: Mapping[str, Any]) -> str:
    note_tweet = raw_tweet.get("note_tweet")
    if isinstance(note_tweet, Mapping) and note_tweet.get("text"):
        text = str(note_tweet["text"])
        entities = note_tweet.get("entities") or {}
    else:
        text = str(raw_tweet.get("text") or "")
        entities = raw_tweet.get("entities") or {}

    text = html.unescape(text)
    quoted_ids = _referenced_tweet_ids(raw_tweet, "quoted")

    if not isinstance(entities, Mapping):
        entities = {}

    for entity in entities.get("urls", []) or []:
        url = entity.get("url")
        if not url:
            continue
        replacement = _url_entity_replacement(entity, quoted_ids)
        text = text.replace(str(url), str(replacement))

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _url_entity_replacement(entity: Mapping[str, Any], quoted_ids: set[str]) -> str:
    if entity.get("media_key"):
        return ""
    if _is_embedded_quote_url(entity, quoted_ids):
        return ""
    expanded_url = str(entity.get("expanded_url") or "").strip()
    display_url = str(entity.get("display_url") or "").strip()
    if expanded_url and len(expanded_url) <= RAW_URL_KEEP_MAX_LENGTH:
        return expanded_url
    return display_url or expanded_url


def _is_embedded_quote_url(entity: Mapping[str, Any], quoted_ids: set[str]) -> bool:
    if not quoted_ids:
        return False
    candidates = [
        str(entity.get("expanded_url") or ""),
        str(entity.get("unwound_url") or ""),
        str(entity.get("display_url") or ""),
    ]
    return any(
        f"/status/{tweet_id}" in candidate or f"/statuses/{tweet_id}" in candidate
        for tweet_id in quoted_ids
        for candidate in candidates
    )


def _referenced_tweet_ids(raw_tweet: Mapping[str, Any], reference_type: str) -> set[str]:
    return {
        str(reference["id"])
        for reference in raw_tweet.get("referenced_tweets", []) or []
        if reference.get("type") == reference_type and reference.get("id") is not None
    }


def _merge_include_list(
    includes: dict[str, Any],
    key: str,
    items: list[Mapping[str, Any]],
    id_key: str,
) -> None:
    target = includes.setdefault(key, [])
    seen = {str(item.get(id_key)) for item in target if item.get(id_key) is not None}
    for item in items:
        item_id = item.get(id_key)
        if item_id is None:
            continue
        item_id = str(item_id)
        if item_id not in seen:
            target.append(copy.deepcopy(dict(item)))
            seen.add(item_id)


def _all_raw_tweets(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tweets = _as_list(payload.get("data"))
    tweets.extend(payload.get("includes", {}).get("tweets", []) or [])
    return tweets


def _as_list(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    return []


def _parse_user(raw: Mapping[str, Any]) -> XUser:
    return XUser(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or raw.get("username") or "Unknown"),
        username=str(raw.get("username") or "unknown"),
        profile_image_url=raw.get("profile_image_url"),
        verified=bool(raw.get("verified") or False),
        verified_type=raw.get("verified_type"),
    )


def _parse_media(raw: Mapping[str, Any]) -> TweetMedia:
    return TweetMedia(
        media_key=str(raw.get("media_key") or ""),
        type=str(raw.get("type") or "photo"),
        url=raw.get("url"),
        preview_image_url=raw.get("preview_image_url"),
        width=_optional_int(raw.get("width")),
        height=_optional_int(raw.get("height")),
        alt_text=raw.get("alt_text"),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unknown_tweet(tweet_id: str) -> Tweet:
    return Tweet(
        id=tweet_id,
        text="",
        author=XUser(id="unknown", name="Unknown", username="unknown"),
    )
