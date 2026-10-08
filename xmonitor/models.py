from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class XUser:
    id: str
    name: str
    username: str
    profile_image_url: str | None = None
    verified: bool = False
    verified_type: str | None = None

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else "@unknown"

    @property
    def high_res_profile_image_url(self) -> str | None:
        if not self.profile_image_url:
            return None
        return self.profile_image_url.replace("_normal.", "_400x400.")


@dataclass(slots=True)
class TweetMedia:
    media_key: str
    type: str
    url: str | None = None
    preview_image_url: str | None = None
    width: int | None = None
    height: int | None = None
    alt_text: str | None = None

    @property
    def best_url(self) -> str | None:
        return self.url or self.preview_image_url


@dataclass(slots=True)
class TweetReference:
    type: str
    tweet: Tweet


@dataclass(slots=True)
class Tweet:
    id: str
    text: str
    author: XUser
    created_at: str | None = None
    media: list[TweetMedia] = field(default_factory=list)
    references: list[TweetReference] = field(default_factory=list)
    translation_text: str | None = None
    is_translated_text: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def first_reference(self, reference_type: str) -> TweetReference | None:
        for reference in self.references:
            if reference.type == reference_type:
                return reference
        return None

    @property
    def repost(self) -> TweetReference | None:
        return self.first_reference("retweeted")

    @property
    def quote(self) -> TweetReference | None:
        return self.first_reference("quoted")
