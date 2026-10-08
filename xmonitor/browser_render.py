from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Tweet, TweetMedia, XUser

TRANSLATION_BLOCK_STYLE = "panel"
TRANSLATION_BLOCK_STYLE_DIVIDER = "divider"
TRANSLATION_BLOCK_STYLE_PANEL = "panel"
MEDIA_MIN_ROW_SHARE = 0.30
MEDIA_HORIZONTAL_RATIO = 1.2
MEDIA_VERTICAL_RATIO = 0.9
MEDIA_MIXED_ROW_PENALTY = 2.0
MEDIA_HORIZONTAL_PAIR_PENALTY = 2.0
MEDIA_INVALID_ROW_PENALTY = 1000.0
BARE_LINK_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    r"(?::\d{1,5})?(?:/[^\s<>'\"]*)?"
)
BARE_LINK_TLDS = {
    "ac",
    "ai",
    "am",
    "app",
    "art",
    "be",
    "biz",
    "cc",
    "cloud",
    "club",
    "co",
    "com",
    "dev",
    "events",
    "fan",
    "fm",
    "fun",
    "gg",
    "gl",
    "icu",
    "id",
    "info",
    "io",
    "jp",
    "la",
    "ly",
    "link",
    "live",
    "me",
    "media",
    "moe",
    "net",
    "news",
    "online",
    "org",
    "page",
    "pm",
    "pro",
    "shop",
    "site",
    "social",
    "store",
    "studio",
    "to",
    "tokyo",
    "tv",
    "work",
    "works",
    "world",
    "xyz",
    "zone",
}
LINK_STOP_CHARS = set(" \t\r\n<>'\"")
LINK_TRAILING_CHARS = set(",!?;:)]}、。！？；：）】」』")

_IMAGE_SETTLE_SCRIPT = """async () => {
    if (document.fonts && document.fonts.ready) {
        await document.fonts.ready;
    }
    const images = Array.from(document.images);
    const waitForImage = (img) => {
        if (img.complete) return Promise.resolve();
        return Promise.race([
            new Promise((resolve) => {
                img.addEventListener('load', resolve, { once: true });
                img.addEventListener('error', resolve, { once: true });
            }),
            new Promise((resolve) => setTimeout(resolve, 2500)),
        ]);
    };
    await Promise.all(images.map(waitForImage));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}"""


@dataclass(frozen=True)
class BrowserRenderConfig:
    width: int = 900
    padding: int = 34
    avatar_size: int = 58
    device_scale_factor: float = 1.2
    timeout_ms: int = 30000
    timezone: str = "Asia/Tokyo"


@dataclass(slots=True)
class MediaLayoutRow:
    items: list[TweetMedia]
    ratios: list[float]


class BrowserTweetRenderer:
    def __init__(self, config: BrowserRenderConfig | None = None) -> None:
        self.config = config or BrowserRenderConfig()

    def render_to_file(self, tweet: Tweet, output_path: str | Path) -> Path:
        from playwright.sync_api import sync_playwright

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        html_doc = render_tweet_html(tweet, self.config)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": self.config.width, "height": 1600},
                    device_scale_factor=self.config.device_scale_factor,
                )
                page.set_content(html_doc, wait_until="networkidle", timeout=self.config.timeout_ms)
                page.evaluate(_IMAGE_SETTLE_SCRIPT)
                page.locator("#capture").screenshot(path=str(output), omit_background=False)
            finally:
                browser.close()

        return output

    def render_to_png_bytes(self, tweet: Tweet) -> bytes:
        from playwright.sync_api import sync_playwright

        html_doc = render_tweet_html(tweet, self.config)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": self.config.width, "height": 1600},
                    device_scale_factor=self.config.device_scale_factor,
                )
                page.set_content(html_doc, wait_until="networkidle", timeout=self.config.timeout_ms)
                page.evaluate(_IMAGE_SETTLE_SCRIPT)
                data = page.locator("#capture").screenshot(omit_background=False)
            finally:
                browser.close()

        if not data:
            raise RuntimeError("Tweet screenshot result is empty")
        return data


class AsyncPlaywrightTweetRenderer:
    """Reuse one Chromium process for low-memory server deployments."""

    def __init__(self, config: BrowserRenderConfig | None = None) -> None:
        self.config = config or BrowserRenderConfig()
        self._playwright = None
        self._browser = None
        self._lock = None

    async def initialize(self) -> None:
        if self._browser is not None:
            return
        from asyncio import Lock
        from playwright.async_api import async_playwright

        self._lock = Lock()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def render_to_file(self, tweet: Tweet, output_path: str | Path) -> Path:
        await self.initialize()
        if self._browser is None or self._lock is None:
            raise RuntimeError("Playwright renderer is not initialized")
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        html_doc = render_tweet_html(tweet, self.config)

        async with self._lock:
            page = await self._browser.new_page(
                viewport={"width": self.config.width, "height": 1600},
                device_scale_factor=self.config.device_scale_factor,
            )
            try:
                await page.set_content(
                    html_doc,
                    wait_until="networkidle",
                    timeout=self.config.timeout_ms,
                )
                await page.evaluate(_IMAGE_SETTLE_SCRIPT)
                await page.locator("#capture").screenshot(
                    path=str(output), omit_background=False
                )
            finally:
                await page.close()
        return output


def render_tweet_html(tweet: Tweet, config: BrowserRenderConfig | None = None) -> str:
    config = config or BrowserRenderConfig()
    translation_block_css = _translation_block_css()
    body = (
        _render_repost(tweet, config)
        if tweet.repost
        else _render_tweet(tweet, compact=False, config=config)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {{
      color-scheme: light;
      --black: #0f1419;
      --gray: #536471;
      --border: #cfd9de;
      --soft: #eff3f4;
      --blue: #1d9bf0;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      background: #fff;
      color: var(--black);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
        Arial, "Noto Sans CJK JP", "Noto Sans CJK SC", "Apple Color Emoji",
        "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji", sans-serif;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }}
    #capture {{
      width: {config.width}px;
      padding: {config.padding}px;
      background: #fff;
    }}
    .repost-indicator {{
      margin: 0 0 8px {config.avatar_size + 14}px;
      color: var(--gray);
      font-size: 18px;
      line-height: 22px;
      font-weight: 600;
    }}
    .tweet {{
      display: grid;
      grid-template-columns: {config.avatar_size}px minmax(0, 1fr);
      column-gap: 14px;
      align-items: start;
    }}
    .tweet.compact {{
      grid-template-columns: 36px minmax(0, 1fr);
      column-gap: 10px;
    }}
    .avatar {{
      width: {config.avatar_size}px;
      height: {config.avatar_size}px;
      border-radius: 999px;
      object-fit: cover;
      background: var(--soft);
      display: block;
    }}
    .avatar-frame {{
      position: relative;
      width: {config.avatar_size}px;
      height: {config.avatar_size}px;
      flex: none;
    }}
    .avatar-frame > .avatar {{
      position: absolute;
      inset: 0;
    }}
    .avatar-frame .avatar-fallback {{
      display: none;
    }}
    .avatar-frame.failed img.avatar {{
      display: none;
    }}
    .avatar-frame.failed .avatar-fallback {{
      display: grid;
    }}
    .compact .avatar {{
      width: 36px;
      height: 36px;
    }}
    .compact .avatar-frame {{
      width: 36px;
      height: 36px;
    }}
    .avatar-fallback {{
      display: grid;
      place-items: center;
      color: var(--gray);
      font-weight: 700;
    }}
    .header {{
      display: flex;
      min-width: 0;
      align-items: baseline;
      gap: 6px;
      line-height: 26px;
      white-space: nowrap;
    }}
    .compact .header {{ line-height: 22px; }}
    .name {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 22px;
      font-weight: 700;
    }}
    .compact .name {{ font-size: 17px; }}
    .handle {{
      flex: none;
      color: var(--gray);
      font-size: 20px;
      font-weight: 400;
    }}
    .timestamp {{
      flex: none;
      color: var(--gray);
      font-size: 20px;
      font-weight: 400;
    }}
    .compact .handle,
    .compact .timestamp {{
      font-size: 16px;
    }}
    .verified {{
      flex: none;
      width: 16px;
      height: 16px;
      border-radius: 999px;
      background: var(--blue);
      color: #fff;
      display: inline-grid;
      place-items: center;
      font-size: 11px;
      font-weight: 800;
      transform: translateY(2px);
    }}
    .text {{
      margin-top: 6px;
      font-size: 24px;
      line-height: 1.36;
      font-weight: 400;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .hashtag,
    .mention,
    .link {{
      color: var(--blue);
    }}
    .text.translated-text,
    .translation {{
      font-family: "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei",
        "Noto Sans CJK JP", -apple-system, BlinkMacSystemFont, "Segoe UI",
        "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji",
        sans-serif;
    }}
    .compact .text {{
      margin-top: 4px;
      font-size: 18px;
      line-height: 1.35;
    }}
    {translation_block_css}
    .media-stack {{
      margin-top: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .media-row {{
      display: flex;
      width: 100%;
      gap: 10px;
      align-items: flex-start;
    }}
    .media-row-single {{
      display: block;
    }}
    .media,
    .media-preview {{
      width: 100%;
      height: auto;
      display: block;
      border-radius: 16px;
      background: #f7f9f9;
      border: 1px solid var(--soft);
    }}
    .media-row-pair > .media,
    .media-row-pair > .media-preview,
    .media-row-pair > .media-placeholder {{
      width: 0;
      min-width: 0;
      flex-basis: 0;
    }}
    .media-placeholder {{
      width: 100%;
      aspect-ratio: var(--ratio, 1.6);
      border-radius: 16px;
      background: #f7f9f9;
      border: 1px solid var(--soft);
    }}
    .media-preview {{
      position: relative;
      overflow: hidden;
    }}
    .media-preview > .media,
    .media-preview > .media-placeholder {{
      border: 0;
      border-radius: 0;
    }}
    .media-badge {{
      position: absolute;
      right: 10px;
      bottom: 10px;
      width: 32px;
      height: 32px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      background: rgba(0, 0, 0, 0.65);
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
    }}
    .quote {{
      margin-top: 12px;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px 26px 14px 14px;
    }}
  </style>
</head>
<body>
  <main id="capture">{body}</main>
</body>
</html>"""


def _translation_block_css() -> str:
    if TRANSLATION_BLOCK_STYLE == TRANSLATION_BLOCK_STYLE_DIVIDER:
        return """
    .translation {
      margin-top: 14px;
      padding-top: 12px;
      position: relative;
      font-size: 24px;
      line-height: 1.36;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .translation::before {
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      width: 90%;
      height: 1px;
      background: var(--border);
    }
    .compact .translation {
      margin-top: 10px;
      padding-top: 10px;
      font-size: 18px;
      line-height: 1.35;
    }
"""
    if TRANSLATION_BLOCK_STYLE != TRANSLATION_BLOCK_STYLE_PANEL:
        raise ValueError(f"Unknown translation block style: {TRANSLATION_BLOCK_STYLE}")
    return """
    .translation {
      margin-top: 12px;
      padding-top: 0;
      font-size: 24px;
      line-height: 1.36;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .translation::before {
      content: none;
    }
    .translation-text {
      display: block;
      width: 100%;
      box-sizing: border-box;
      padding: 14px 16px;
      border-radius: 16px;
      background: #eef6fb;
      border: 1px solid #eff3f4;
    }
    .compact .translation {
      margin-top: 8px;
      padding-top: 0;
      font-size: 18px;
      line-height: 1.35;
    }
    .compact .translation-text {
      padding: 12px 14px;
    }
"""


def _render_repost(tweet: Tweet, config: BrowserRenderConfig) -> str:
    repost = tweet.repost
    if repost is None:
        return _render_tweet(tweet, compact=False, config=config)
    return (
        f'<div class="repost-indicator">{_escape(tweet.author.name)} reposted</div>'
        f"{_render_tweet(repost.tweet, compact=False, config=config)}"
    )


def _render_tweet(tweet: Tweet, *, compact: bool, config: BrowserRenderConfig) -> str:
    compact_class = " compact" if compact else ""
    quote = (
        f'<div class="quote">{_render_tweet(tweet.quote.tweet, compact=True, config=config)}</div>'
        if tweet.quote and not compact
        else ""
    )
    return f"""
    <article class="tweet{compact_class}">
      {_render_avatar(tweet.author)}
      <div class="body">
        {_render_header(tweet, config)}
        {_render_text(tweet.text, translated=tweet.is_translated_text)}
        {_render_translation(tweet.translation_text)}
        {_render_media_stack(tweet.media)}
        {quote}
      </div>
    </article>
    """


def _render_header(tweet: Tweet, config: BrowserRenderConfig) -> str:
    user = tweet.author
    verified = '<span class="verified">✓</span>' if user.verified else ""
    timestamp = _format_created_at(tweet.created_at, config.timezone)
    time_html = f'<span class="timestamp">· {_escape(timestamp)}</span>' if timestamp else ""
    return (
        '<div class="header">'
        f'<span class="name">{_escape(user.name)}</span>'
        f"{verified}"
        f'<span class="handle">{_escape(user.handle)}</span>'
        f"{time_html}"
        "</div>"
    )


def _render_avatar(user: XUser) -> str:
    url = user.high_res_profile_image_url
    initial = _escape((user.name or user.username or "?")[:1].upper())
    if not url:
        return f'<div class="avatar avatar-fallback">{initial}</div>'
    return (
        '<span class="avatar-frame">'
        f'<img class="avatar" src="{_attr(url)}" alt="" '
        "onerror=\"this.parentElement.classList.add('failed');"
        'this.removeAttribute(\'onerror\');\">'
        f'<span class="avatar avatar-fallback">{initial}</span>'
        "</span>"
    )


def _render_text(text: str, translated: bool = False) -> str:
    if not text:
        return ""
    class_name = "text translated-text" if translated else "text"
    return f'<div class="{class_name}">{_render_text_entities(text)}</div>'


def _render_translation(text: str | None) -> str:
    if not text:
        return ""
    return (
        '<section class="translation" lang="zh-CN">'
        f'<div class="translation-text">{_render_text_entities(text)}</div>'
        "</section>"
    )


def _render_media_stack(media: list[TweetMedia]) -> str:
    items = [item for item in media if item.type in {"photo", "video", "animated_gif"}]
    if not items:
        return ""
    rows = _build_media_layout(items)
    row_html = "".join(_render_media_row(row) for row in rows)
    return '<div class="media-stack">' + row_html + "</div>"


def _build_media_layout(media: list[TweetMedia]) -> list[MediaLayoutRow]:
    if not 1 <= len(media) <= 4:
        raise ValueError("X posts support 1 to 4 media items")
    ratios = [_media_ratio(item) for item in media]
    candidates = _media_layout_candidates(len(media))
    best_candidate = min(candidates, key=lambda sizes: _score_media_layout(sizes, ratios))
    rows: list[MediaLayoutRow] = []
    start = 0
    for size in best_candidate:
        end = start + size
        rows.append(MediaLayoutRow(items=media[start:end], ratios=ratios[start:end]))
        start = end
    return rows


def _media_layout_candidates(count: int) -> list[list[int]]:
    if count == 1:
        return [[1]]
    if count == 2:
        return [[2], [1, 1]]
    if count == 3:
        return [[2, 1], [1, 2], [1, 1, 1]]
    if count == 4:
        return [[2, 2], [2, 1, 1], [1, 2, 1], [1, 1, 2], [1, 1, 1, 1]]
    raise ValueError("Media count must be between 1 and 4")


def _score_media_layout(row_sizes: list[int], ratios: list[float]) -> float:
    if sum(row_sizes) != len(ratios):
        raise ValueError("Media layout does not match media count")
    score = 0.0
    start = 0
    for size in row_sizes:
        row_ratios = ratios[start : start + size]
        score += _score_media_row(row_ratios)
        start += size
    return score


def _score_media_row(ratios: list[float]) -> float:
    if not 1 <= len(ratios) <= 2:
        raise ValueError("A media row supports one or two items")
    ratio_sum = sum(ratios)
    if ratio_sum <= 0:
        raise ValueError("Media ratio sum must be positive")
    score = 1.0 / ratio_sum
    if len(ratios) == 1:
        return score

    shares = [ratio / ratio_sum for ratio in ratios]
    if min(shares) < MEDIA_MIN_ROW_SHARE:
        score += MEDIA_INVALID_ROW_PENALTY
    if all(ratio >= MEDIA_HORIZONTAL_RATIO for ratio in ratios):
        score += MEDIA_HORIZONTAL_PAIR_PENALTY
    if _is_mixed_orientation_row(ratios):
        score += MEDIA_MIXED_ROW_PENALTY
    return score


def _is_mixed_orientation_row(ratios: list[float]) -> bool:
    has_horizontal = any(ratio >= MEDIA_HORIZONTAL_RATIO for ratio in ratios)
    has_vertical = any(ratio <= MEDIA_VERTICAL_RATIO for ratio in ratios)
    return has_horizontal and has_vertical


def _render_media_row(row: MediaLayoutRow) -> str:
    pair = len(row.items) > 1
    class_name = "media-row media-row-pair" if pair else "media-row media-row-single"
    html_items = [
        _render_media(item, flex_grow=ratio if pair else None)
        for item, ratio in zip(row.items, row.ratios, strict=True)
    ]
    return f'<div class="{class_name}">' + "".join(html_items) + "</div>"


def _render_media(item: TweetMedia, flex_grow: float | None = None) -> str:
    """渲染媒体，并为视频和 GIF 预览图叠加右下角类型标识。

    Args:
        item (TweetMedia): 媒体对象。
        flex_grow (float | None): 多图同行时使用的 flex-grow 值。

    Returns:
        str: HTML 片段。

    Raises:
        ValueError: 当 flex_grow 非正数时抛出。
    """
    url = item.best_url
    has_badge = item.type in {"video", "animated_gif"}
    flex_style = _media_flex_style(flex_grow)
    image_flex_style = "" if has_badge else flex_style
    if url and urlparse(url).scheme not in {"", "placeholder"}:
        alt = _attr(item.alt_text or "")
        content = f'<img class="media" src="{_attr(url)}" alt="{alt}"{image_flex_style}>'
    else:
        ratio = _media_ratio(item)
        styles = [f"--ratio:{ratio:.4f}"]
        if flex_grow is not None and not has_badge:
            styles.append(f"flex-grow:{flex_grow:.6f}")
        content = f'<div class="media-placeholder" style="{_attr(";".join(styles))}"></div>'
    if not has_badge:
        return content
    label = "GIF"
    badge = "GIF"
    if item.type == "video":
        label = "视频"
        badge = (
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<rect x="3" y="6" width="13" height="12" rx="2"/>'
            '<path d="m16 9 5-3v12l-5-3"/>'
            '</svg>'
        )
    return (
        f'<div class="media-preview"{flex_style}>{content}'
        f'<span class="media-badge" role="img" aria-label="{label}">'
        f'{badge}</span></div>'
    )


def _media_flex_style(flex_grow: float | None) -> str:
    if flex_grow is None:
        return ""
    if flex_grow <= 0:
        raise ValueError("flex_grow must be positive")
    return f' style="flex-grow:{flex_grow:.6f}"'


def _media_ratio(item: TweetMedia) -> float:
    if item.width and item.height and item.width > 0 and item.height > 0:
        return max(0.4, min(2.5, item.width / item.height))
    return 1.6


def _format_created_at(created_at: str | None, timezone_name: str) -> str | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    local = created.astimezone(tz)
    return f"{local.year}年{local.month}月{local.day}日 {local.hour:02d}:{local.minute:02d}"


def _render_text_entities(text: str) -> str:
    parts: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        link_end = _find_link_end(text, index)
        if link_end > index:
            link = _escape(text[index:link_end])
            parts.append(f'<span class="link">{link}</span>')
            index = link_end
            continue

        if text[index] == "#" and _is_entity_start(text, index, _is_hashtag_char):
            end = _find_entity_end(text, index, _is_hashtag_char)
            if end > index + 1:
                tag = _escape(text[index:end])
                parts.append(f'<span class="hashtag">{tag}</span>')
                index = end
                continue

        if text[index] == "@" and _is_entity_start(text, index, _is_mention_char):
            end = _find_mention_end(text, index)
            if end > index + 1:
                mention = _escape(text[index:end])
                parts.append(f'<span class="mention">{mention}</span>')
                index = end
                continue

        parts.append(_escape(text[index]))
        index += 1

    return "".join(parts)


def _find_link_end(text: str, index: int) -> int:
    if not _is_link_start_boundary(text, index):
        return index
    lowered = text.lower()
    if lowered.startswith("http://", index):
        end = _find_scheme_link_end(text, index, len("http://"))
        return _trim_link_end(text, index, end)
    if lowered.startswith("https://", index):
        end = _find_scheme_link_end(text, index, len("https://"))
        return _trim_link_end(text, index, end)
    if lowered.startswith("www.", index):
        match = BARE_LINK_PATTERN.match(text, index)
        if match is None:
            return index
        candidate = text[index : match.end()]
        if not _is_valid_bare_link(candidate, allow_unknown_tld=True):
            return index
        return _trim_link_end(text, index, match.end())
    match = BARE_LINK_PATTERN.match(text, index)
    if match is None:
        return index
    candidate = text[index : match.end()]
    if not _is_valid_bare_link(candidate):
        return index
    return _trim_link_end(text, index, match.end())


def _is_link_start_boundary(text: str, index: int) -> bool:
    if index == 0:
        return True
    previous = text[index - 1]
    if previous.isspace() or previous in "([{\"'「『【<":
        return True
    return not _is_ascii_link_inner_char(previous)


def _find_scheme_link_end(text: str, index: int, scheme_length: int) -> int:
    end = index
    while end < len(text) and text[end] not in LINK_STOP_CHARS:
        end += 1
    if end <= index + scheme_length:
        return index
    return end


def _trim_link_end(text: str, index: int, end: int) -> int:
    while end > index:
        tail = text[end - 1]
        if tail == "." and text[max(index, end - 3) : end] != "...":
            end -= 1
            continue
        if tail in LINK_TRAILING_CHARS:
            end -= 1
            continue
        break
    return end


def _is_valid_bare_link(value: str, allow_unknown_tld: bool = False) -> bool:
    host = value.split("/", 1)[0].split(":", 1)[0]
    labels = host.split(".")
    if len(labels) < 2:
        return False
    tld = labels[-1]
    if len(tld) < 2 or not tld.isalpha():
        return False
    if allow_unknown_tld:
        return True
    return tld.lower() in BARE_LINK_TLDS


def _is_ascii_link_inner_char(char: str) -> bool:
    if char in {"@", ".", "-", "_"}:
        return True
    if "0" <= char <= "9":
        return True
    if "A" <= char <= "Z":
        return True
    if "a" <= char <= "z":
        return True
    return False


def _is_entity_start(text: str, index: int, char_checker: Callable[[str], bool]) -> bool:
    if index == 0:
        return True
    return not char_checker(text[index - 1])


def _find_entity_end(text: str, index: int, char_checker: Callable[[str], bool]) -> int:
    end = index + 1
    while end < len(text) and char_checker(text[end]):
        end += 1
    return end


def _find_mention_end(text: str, index: int) -> int:
    end = _find_entity_end(text, index, _is_mention_char)
    if end - index > 16:
        return index + 16
    return end


def _is_mention_char(char: str) -> bool:
    if char == "_":
        return True
    if "0" <= char <= "9":
        return True
    if "A" <= char <= "Z":
        return True
    if "a" <= char <= "z":
        return True
    return False


def _is_hashtag_char(char: str) -> bool:
    if char in {"_", "\u30fc", "\uff70"}:
        return True
    category = unicodedata.category(char)
    return category[0] in {"L", "N"} or category in {"Mn", "Mc"}


def _escape(value: str) -> str:
    return html.escape(value or "", quote=False)


def _attr(value: str) -> str:
    return html.escape(value or "", quote=True)
