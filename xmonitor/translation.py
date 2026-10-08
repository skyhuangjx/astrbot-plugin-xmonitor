from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any, Protocol

from .models import Tweet

TRANSLATION_MODE_ENV = "XMONITOR_TRANSLATION_MODE"
LEGACY_TRANSLATION_MODE_ENV = "X_MONITOR_TRANSLATION_MODE"
DEFAULT_TRANSLATION_MODEL = "gemini-3.1-flash-lite"


class TranslationMode:
    NONE = "none"
    TRANSLATED = "translated"
    BILINGUAL = "bilingual"

    @classmethod
    def normalize(cls, value: str | None) -> str:
        raw = (value or "").strip().lower()
        if raw in {"", "0", "off", "none", "no", "false", "original", "不翻译"}:
            return cls.NONE
        if raw in {"1", "translated", "translate", "only", "zh", "zh-cn", "仅翻译"}:
            return cls.TRANSLATED
        if raw in {"2", "bilingual", "compare", "dual", "both", "对照", "双语"}:
            return cls.BILINGUAL
        raise ValueError(
            f"Unsupported translation mode: {value}. Use none, translated, or bilingual."
        )

    @classmethod
    def from_env(cls) -> str:
        value = os.environ.get(TRANSLATION_MODE_ENV)
        if value is None:
            value = os.environ.get(LEGACY_TRANSLATION_MODE_ENV)
        return cls.normalize(value)


class TweetTextTranslator(Protocol):
    def translate_texts(self, texts: Sequence[str]) -> list[str]:
        ...


class GeminiTweetTranslator:
    def __init__(
        self,
        client: Any | None = None,
        model: str = DEFAULT_TRANSLATION_MODEL,
    ) -> None:
        model = model.strip()
        if not model:
            raise ValueError("Gemini translation model is required")
        self._client = client
        self._model = model

    def translate_texts(self, texts: Sequence[str]) -> list[str]:
        originals = [text.strip() for text in texts]
        if not originals:
            raise ValueError("No tweet text was provided for translation")
        if not all(originals):
            raise ValueError("Tweet text for translation cannot be empty")

        client = self._client or self._create_client()
        response = client.models.generate_content(
            model=self._model,
            contents=self._build_prompt(originals),
            config=self._build_config(),
        )
        payload = json.loads(self._response_text(response))
        translations = payload.get("translations")
        if not isinstance(translations, list):
            raise ValueError("Gemini translation response must contain translations")
        if len(translations) != len(originals):
            raise ValueError("Gemini translation count does not match input count")

        result = [str(item).strip() for item in translations]
        if not all(result):
            raise ValueError("Gemini translation response contains empty text")
        return result

    def _create_client(self) -> Any:
        try:
            from google import genai
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Translation requires the google-genai package. "
                "Install xmonitor with the translate extra."
            ) from exc

        api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY")
            or ""
        ).strip()
        if api_key:
            return genai.Client(api_key=api_key)

        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower()
        project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        location = (os.environ.get("GOOGLE_CLOUD_LOCATION") or "").strip()
        if use_vertex in {"1", "true", "yes", "on"} and project and location:
            return genai.Client(vertexai=True, project=project, location=location)

        raise RuntimeError(
            "Missing Gemini credentials. Set GOOGLE_API_KEY, GEMINI_API_KEY, "
            "GOOGLE_GENERATIVE_AI_API_KEY, or Vertex AI environment variables."
        )

    def _build_config(self) -> Any:
        from google.genai import types

        try:
            return types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            )
        except TypeError:
            return types.GenerateContentConfig(
                temperature=0,
                responseMimeType="application/json",
            )

    def _build_prompt(self, texts: Sequence[str]) -> str:
        payload = json.dumps(list(texts), ensure_ascii=False)
        return (
            "Translate each X/Twitter post text in the JSON array to Simplified Chinese. "
            "Preserve URLs, @usernames, hashtags, emoji, and proper nouns. "
            "Return only a JSON object in exactly this shape: "
            "{\"translations\":[...]}. The array length must match the input.\n"
            f"Input: {payload}"
        )

    def _response_text(self, response: Any) -> str:
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

        candidates = list(getattr(response, "candidates", None) or [])
        if not candidates:
            raise ValueError("Gemini translation response has no candidates")
        content = getattr(candidates[0], "content", None)
        parts = list(getattr(content, "parts", None) or [])
        texts = [
            str(part_text).strip()
            for part in parts
            for part_text in [getattr(part, "text", None)]
            if isinstance(part_text, str) and part_text.strip()
        ]
        if not texts:
            raise ValueError("Gemini translation response has no text")
        return "\n".join(texts)


class TweetTranslationApplier:
    def __init__(self, translator: TweetTextTranslator | None = None) -> None:
        self._translator = translator or GeminiTweetTranslator()

    def apply(self, tweet: Tweet, mode: str | None) -> None:
        normalized = TranslationMode.normalize(mode)
        if normalized == TranslationMode.NONE:
            return

        targets = [item for item in self._collect_tweets(tweet) if item.text.strip()]
        if not targets:
            return

        originals = [item.text for item in targets]
        translations = self._translator.translate_texts(originals)
        if len(translations) != len(targets):
            raise ValueError("Translation count does not match tweet count")

        for target, original, translation in zip(targets, originals, translations, strict=True):
            self._apply_translation(target, original, translation, normalized)

    def _collect_tweets(self, tweet: Tweet) -> list[Tweet]:
        ordered: list[Tweet] = []
        seen: set[str] = set()

        def visit(current: Tweet) -> None:
            if current.id in seen:
                return
            seen.add(current.id)
            ordered.append(current)
            for reference in current.references:
                visit(reference.tweet)

        visit(tweet)
        return ordered

    def _apply_translation(self, tweet: Tweet, original: str, translation: str, mode: str) -> None:
        if mode == TranslationMode.TRANSLATED:
            tweet.text = translation.strip()
            tweet.translation_text = None
            tweet.is_translated_text = True
            return
        if mode == TranslationMode.BILINGUAL:
            tweet.text = original.strip()
            tweet.translation_text = translation.strip()
            tweet.is_translated_text = False
            return
        raise ValueError(f"Unsupported translation mode: {mode}")
