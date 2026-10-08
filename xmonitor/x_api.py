from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from .parser import collect_referenced_tweet_ids, merge_api_payloads


DEFAULT_BASE_URL = "https://api.x.com/2"

TWEET_FIELDS = [
    "attachments",
    "author_id",
    "created_at",
    "entities",
    "note_tweet",
    "referenced_tweets",
]
USER_FIELDS = ["name", "profile_image_url", "username", "verified", "verified_type"]
MEDIA_FIELDS = [
    "alt_text",
    "height",
    "media_key",
    "preview_image_url",
    "type",
    "url",
    "width",
]
EXPANSIONS = [
    "author_id",
    "attachments.media_keys",
    "referenced_tweets.id",
    "referenced_tweets.id.author_id",
]


class XApiError(RuntimeError):
    """An expected X API failure that can be shown to a user safely."""


class AsyncXApiClient:
    """Small async client for the X API v2 endpoints used by XMonitor."""

    def __init__(
        self,
        bearer_token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not bearer_token.strip():
            raise ValueError("bearer_token is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {bearer_token.strip()}",
                "User-Agent": "AstrBot-XMonitor/1.0",
            },
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def get_user_by_username(self, username: str) -> dict[str, Any]:
        return await self._get(
            f"/users/by/username/{username.lstrip('@')}",
            params={"user.fields": ",".join(USER_FIELDS)},
        )

    async def get_user_id(self, username: str) -> str:
        payload = await self.get_user_by_username(username)
        user_id = payload.get("data", {}).get("id")
        if not user_id:
            raise XApiError(f"无法解析用户 @{username.lstrip('@')}")
        return str(user_id)

    async def get_latest_tweet_payload(
        self,
        username: str,
        *,
        include_replies: bool = False,
        include_retweets: bool = True,
        since_id: str | None = None,
        max_results: int = 10,
    ) -> dict[str, Any]:
        user_id = await self.get_user_id(username)
        return await self.get_user_timeline_payload(
            user_id,
            username=username,
            include_replies=include_replies,
            include_retweets=include_retweets,
            since_id=since_id,
            max_results=max_results,
        )

    async def get_user_timeline_payload(
        self,
        user_id: str,
        *,
        username: str | None = None,
        include_replies: bool = False,
        include_retweets: bool = True,
        since_id: str | None = None,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Fetch a user's timeline, optionally starting after a Post ID."""

        exclude: list[str] = []
        if not include_replies:
            exclude.append("replies")
        if not include_retweets:
            exclude.append("retweets")

        params: dict[str, Any] = self._tweet_params()
        params["max_results"] = str(max(5, min(100, int(max_results))))
        if since_id and since_id.isdigit():
            params["since_id"] = since_id
        if exclude:
            params["exclude"] = ",".join(exclude)

        payload = await self._get_with_note_tweet_retry(
            f"/users/{user_id}/tweets", params=params
        )
        if not payload.get("data"):
            label = f"@{username.lstrip('@')}" if username else user_id
            raise XApiError(f"{label} 没有可用推文")
        return await self._hydrate_references(payload)

    async def get_tweet_payload(self, tweet_id: str) -> dict[str, Any]:
        payload = await self.get_tweets_payload([tweet_id])
        if not payload.get("data"):
            raise XApiError(f"没有找到推文 {tweet_id}")
        return await self._hydrate_references(payload)

    async def get_tweets_payload(self, tweet_ids: list[str]) -> dict[str, Any]:
        params = self._tweet_params()
        params["ids"] = ",".join(tweet_ids[:100])
        return await self._get_with_note_tweet_retry("/tweets", params=params)

    async def _hydrate_references(
        self, payload: dict[str, Any], *, max_rounds: int = 3
    ) -> dict[str, Any]:
        fetched_for_media: set[str] = set()
        for _ in range(max_rounds):
            present_ids = _payload_tweet_ids(payload)
            referenced_ids = [
                tweet_id
                for tweet_id in collect_referenced_tweet_ids(payload)
                if tweet_id not in present_ids
            ]
            missing_media_tweet_ids = [
                tweet_id
                for tweet_id in _tweet_ids_with_missing_media(payload)
                if tweet_id not in fetched_for_media
            ]
            ids_to_fetch = _unique_ids([*referenced_ids, *missing_media_tweet_ids])
            if not ids_to_fetch:
                return payload
            payload = merge_api_payloads(
                payload, await self.get_tweets_payload(ids_to_fetch)
            )
            fetched_for_media.update(missing_media_tweet_ids)
        return payload

    def _tweet_params(self) -> dict[str, str]:
        return {
            "tweet.fields": ",".join(TWEET_FIELDS),
            "user.fields": ",".join(USER_FIELDS),
            "media.fields": ",".join(MEDIA_FIELDS),
            "expansions": ",".join(EXPANSIONS),
        }

    async def _get_with_note_tweet_retry(
        self, path: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            return await self._get(path, params=params)
        except XApiError as exc:
            if "note_tweet" not in str(exc):
                raise
            retry_params = dict(params)
            retry_params["tweet.fields"] = ",".join(
                field for field in TWEET_FIELDS if field != "note_tweet"
            )
            return await self._get(path, params=retry_params)

    async def _get(
        self, path: str, *, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise XApiError("X API 请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise XApiError("连接 X API 失败，请检查网络") from exc

        if response.status_code >= 400:
            raise XApiError(_format_api_error(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise XApiError("X API 返回了无法解析的响应") from exc
        if not isinstance(payload, dict):
            raise XApiError("X API 返回了无效响应")
        return payload


def _format_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"X API HTTP {response.status_code}"

    errors = payload.get("errors") or []
    if errors:
        details = "; ".join(
            str(error.get("detail") or error.get("title") or error)
            for error in errors[:3]
        )
    else:
        details = str(payload.get("detail") or payload.get("title") or "请求失败")[:300]
    return f"X API HTTP {response.status_code}: {details}"


def _payload_tweet_ids(payload: Mapping[str, Any]) -> set[str]:
    return {
        str(tweet["id"])
        for tweet in _payload_raw_tweets(payload)
        if tweet.get("id") is not None
    }


def _tweet_ids_with_missing_media(payload: Mapping[str, Any]) -> list[str]:
    present_media_keys = _payload_media_keys(payload)
    ids: list[str] = []
    seen: set[str] = set()
    for tweet in _payload_raw_tweets(payload):
        tweet_id = tweet.get("id")
        media_keys = _tweet_media_keys(tweet)
        if tweet_id is None or not media_keys:
            continue
        if all(media_key in present_media_keys for media_key in media_keys):
            continue
        normalized = str(tweet_id)
        if normalized not in seen:
            ids.append(normalized)
            seen.add(normalized)
    return ids


def _payload_media_keys(payload: Mapping[str, Any]) -> set[str]:
    includes = payload.get("includes")
    if not isinstance(includes, Mapping):
        return set()
    media = includes.get("media") or []
    return {
        str(item["media_key"])
        for item in media
        if isinstance(item, Mapping) and item.get("media_key") is not None
    }


def _tweet_media_keys(tweet: Mapping[str, Any]) -> list[str]:
    attachments = tweet.get("attachments")
    if not isinstance(attachments, Mapping):
        return []
    media_keys = attachments.get("media_keys") or []
    return [str(key) for key in media_keys] if isinstance(media_keys, list) else []


def _payload_raw_tweets(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tweets: list[Mapping[str, Any]] = []
    data = payload.get("data")
    if isinstance(data, list):
        tweets.extend(tweet for tweet in data if isinstance(tweet, Mapping))
    elif isinstance(data, Mapping) and data.get("id") is not None:
        tweets.append(data)
    includes = payload.get("includes")
    if isinstance(includes, Mapping):
        included = includes.get("tweets") or []
        if isinstance(included, list):
            tweets.extend(tweet for tweet in included if isinstance(tweet, Mapping))
    return tweets


def _unique_ids(ids: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tweet_id in ids:
        if tweet_id not in seen:
            result.append(tweet_id)
            seen.add(tweet_id)
    return result
