from __future__ import annotations

from .models import Tweet, TweetMedia, TweetReference, XUser


def sample_tweet() -> Tweet:
    """Return a local sample so an installation can be checked without an API token."""
    author = XUser(
        id="sample-author",
        name="XMonitor 示例",
        username="xmonitor_demo",
        verified=True,
    )
    quoted_author = XUser(
        id="quoted-author",
        name="示例引用用户",
        username="quoted_demo",
    )
    quoted = Tweet(
        id="sample-quoted",
        text="这是被引用的示例推文。#XMonitor",
        author=quoted_author,
    )
    return Tweet(
        id="sample",
        text="XMonitor 已经连接到 AstrBot。\n发送真实的 X 链接即可渲染。",
        author=author,
        created_at="2026-05-07T05:15:00.000Z",
        media=[
            TweetMedia(
                media_key="sample-image",
                type="photo",
                url=(
                    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'"
                    "%20width='1200'%20height='800'%3E%3Crect%20width='100%25'"
                    "%20height='100%25'%20fill='%231d9bf0'/%3E%3Ctext%20x='600'"
                    "%20y='420'%20text-anchor='middle'%20font-size='80'"
                    "%20fill='white'%3EXMonitor%3C/text%3E%3C/svg%3E"
                ),
                width=1200,
                height=800,
            )
        ],
        references=[TweetReference(type="quoted", tweet=quoted)],
    )
