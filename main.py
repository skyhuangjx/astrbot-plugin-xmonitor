from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_session import MessageSession

from .xmonitor.browser_render import (
    AsyncPlaywrightTweetRenderer,
    BrowserRenderConfig,
    render_tweet_html,
)
from .xmonitor.models import Tweet
from .xmonitor.monitoring import MonitorStore, edge_key, normalize_graph
from .xmonitor.parser import parse_tweet_response
from .xmonitor.sample import sample_tweet
from .xmonitor.x_api import AsyncXApiClient, XApiError


PLUGIN_NAME = "xmonitor"
TRANSLATION_MODES = {"none", "translated", "bilingual"}


@star.register(
    PLUGIN_NAME,
    "skyhuangjx",
    "监控 X 账号并将新推文渲染成图片发送到 AstrBot 会话",
    "2.0.6",
)
class XMonitorPlugin(star.Star):
    """XMonitor v2: graph-configured monitoring for AstrBot."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._api: AsyncXApiClient | None = None
        self._monitor_task: asyncio.Task | None = None
        self._playwright_renderer: AsyncPlaywrightTweetRenderer | None = None
        self._stop_event = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._graph_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._store = MonitorStore(
            Path("data") / "plugin_data" / "astrbot_plugin_xmonitor" / "monitor.json"
        )
        self._output_dir = self._store.path.parent / "renders"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._last_run: str | None = None
        self._last_errors: dict[str, str] = {}
        self._user_ids: dict[str, str] = {}
        self._register_web_api()

    async def initialize(self) -> None:
        token = str(self.config.get("bearer_token") or "").strip()
        if token:
            self._api = AsyncXApiClient(
                token,
                base_url=str(self.config.get("api_base_url") or "https://api.x.com/2"),
                timeout=float(self.config.get("request_timeout", 20)),
            )
        else:
            self.logger.warning("XMonitor 未配置 bearer_token，监控任务保持暂停")
        self._stop_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def terminate(self) -> None:
        self._stop_event.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
        if self._api is not None:
            await self._api.aclose()
            self._api = None
        if self._playwright_renderer is not None:
            await self._playwright_renderer.close()
            self._playwright_renderer = None

    @filter.command("xmonitor", alias={"x"})
    async def xmonitor(self, event: AstrMessageEvent, target: str = ""):
        """查询一条推文、查看监控状态或显示当前 AstrBot 会话信息。"""
        raw_command = event.message_str.strip()
        argument = self._extract_argument(raw_command, target)
        if not argument:
            yield event.plain_result(_help_text())
            return

        mode, argument = _split_translation_mode(argument)
        mode = mode or self._translation_mode()
        if mode not in TRANSLATION_MODES:
            yield event.plain_result("翻译模式只能是 none、translated 或 bilingual")
            return

        command = argument.lower()
        if command in {"help", "帮助"}:
            yield event.plain_result(_help_text())
            return
        if command in {"where", "目标", "会话"}:
            yield event.plain_result(_session_text(event))
            return
        if command in {"status", "状态"}:
            yield event.plain_result(_status_text(self._status_payload()))
            return
        if command in {"check", "检查"}:
            await self._run_monitor_cycle()
            yield event.plain_result(_status_text(self._status_payload()))
            return

        is_sample = command in {"sample", "示例", "test", "测试"}
        operation_id = uuid.uuid4().hex[:8]
        source = (
            "sample"
            if is_sample
            else "manual_latest"
            if argument.lower().startswith("latest")
            else "manual_tweet"
        )
        started = time.perf_counter()
        stage = "prepare"
        self.logger.info(
            "XMonitor request started op=%s source=%s mode=%s",
            operation_id,
            source,
            mode,
        )
        if self._api is None and not is_sample:
            yield event.plain_result(
                "XMonitor 尚未配置 X API Bearer Token，请在插件配置中填写 bearer_token。"
            )
            return

        try:
            stage = "fetch"
            tweet = sample_tweet() if is_sample else await self._fetch_tweet(argument)
            self.logger.info(
                "XMonitor fetch finished op=%s source=%s tweet=%s elapsed=%.2fs",
                operation_id,
                source,
                tweet.id,
                time.perf_counter() - started,
            )
            stage = "translate"
            if mode != "none":
                await self._apply_translation(tweet, mode, event.unified_msg_origin)
            stage = "render"
            output_path, _ = await self._render(tweet)
            try:
                output_size = output_path.stat().st_size
            except OSError:
                output_size = -1
            self.logger.info(
                "XMonitor render finished op=%s source=%s elapsed=%.2fs size=%s",
                operation_id,
                source,
                time.perf_counter() - started,
                output_size,
            )
        except (XApiError, ValueError, RuntimeError) as exc:
            self.logger.warning(
                "XMonitor request failed op=%s source=%s stage=%s elapsed=%.2fs: %s",
                operation_id,
                source,
                stage,
                time.perf_counter() - started,
                str(exc)[:300],
            )
            yield event.plain_result(f"XMonitor 处理失败：{exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "XMonitor unexpected failure op=%s source=%s stage=%s elapsed=%.2fs",
                operation_id,
                source,
                stage,
                time.perf_counter() - started,
            )
            yield event.plain_result("XMonitor 处理失败，请查看 AstrBot 日志")
            return

        self.logger.info(
            "XMonitor image prepared op=%s source=%s tweet=%s elapsed=%.2fs path=%s",
            operation_id,
            source,
            tweet.id,
            time.perf_counter() - started,
            output_path.name,
        )
        yield event.image_result(str(output_path))

    async def _fetch_tweet(self, argument: str) -> Tweet:
        if self._api is None:
            raise XApiError("X API 客户端未初始化")
        latest_match = re.match(r"^latest(?:\s+(.+))?$", argument.strip(), re.IGNORECASE)
        if latest_match:
            username = (latest_match.group(1) or "").strip().lstrip("@")
            if not username:
                raise ValueError("用法：/xmonitor latest @用户名")
            _validate_username(username)
            payload = await self._api.get_latest_tweet_payload(username)
        else:
            tweet_id = _extract_tweet_id(argument)
            payload = await self._api.get_tweet_payload(tweet_id)
        tweets = parse_tweet_response(payload)
        if not tweets:
            raise XApiError("X API 没有返回可渲染的推文")
        return tweets[0]

    async def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_monitor_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("XMonitor monitor cycle failed")
            interval = self._int_config("poll_interval_minutes", 10, 1, 1440)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval * 60)
            except asyncio.TimeoutError:
                pass

    async def _run_monitor_cycle(self) -> bool:
        if self._api is None or self._check_lock.locked():
            return False
        async with self._check_lock:
            self._last_run = _now_text()
            graph = await self._load_graph()
            accounts = {
                item["id"]: item for item in graph["accounts"] if item["enabled"]
            }
            targets = {item["id"]: item for item in graph["targets"]}
            by_account: dict[str, list[dict[str, str]]] = {}
            for binding in graph["bindings"]:
                account = accounts.get(binding["account_id"])
                target = targets.get(binding["target_id"])
                if account and target:
                    by_account.setdefault(account["id"], []).append(target)

            for account_id, account_targets in by_account.items():
                try:
                    await self._check_account(
                        accounts[account_id], account_targets, graph
                    )
                    self._last_errors.pop(accounts[account_id]["username"], None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    username = accounts[account_id]["username"]
                    self._last_errors[username] = str(exc)[:300]
                    self.logger.warning(
                        "XMonitor account @%s failed: %s", username, str(exc)[:300]
                    )
            await self._cleanup_old_outputs()
        return True

    async def _check_account(
        self,
        account: dict[str, object],
        account_targets: list[dict[str, str]],
        graph: dict[str, object],
    ) -> None:
        if self._api is None:
            return
        username = str(account["username"])
        account_id = str(account["id"])
        target_ids = [str(target["id"]) for target in account_targets]
        cursors = graph.get("cursors", {})
        if not isinstance(cursors, dict):
            cursors = {}
        edge_cursors = {
            target_id: str(cursors.get(edge_key(account_id, target_id), ""))
            for target_id in target_ids
        }
        user_id = self._user_ids.get(username)
        if user_id is None:
            user_id = await self._api.get_user_id(username)
            self._user_ids[username] = user_id
        try:
            payload = await self._api.get_user_timeline_payload(
                user_id,
                username=username,
                since_id=_minimum_cursor(edge_cursors.values()),
                max_results=10,
            )
        except XApiError as exc:
            if "没有可用推文" in str(exc):
                return
            raise
        tweets = parse_tweet_response(payload)
        tweets = sorted(
            (tweet for tweet in tweets if tweet.id.isdigit()),
            key=lambda tweet: int(tweet.id),
        )
        if not tweets:
            return
        newest_id = tweets[-1].id

        # A newly added edge starts from now and does not replay old posts.
        for target_id, cursor in edge_cursors.items():
            if not cursor:
                await self._set_cursor(edge_key(account_id, target_id), newest_id)
                edge_cursors[target_id] = newest_id

        mode = self._translation_mode()
        targets_by_id = {str(target["id"]): target for target in account_targets}
        for tweet in tweets:
            pending_targets = [
                targets_by_id[target_id]
                for target_id, cursor in edge_cursors.items()
                if cursor.isdigit() and int(tweet.id) > int(cursor)
            ]
            if not pending_targets:
                continue
            if mode != "none":
                target_session = _target_session(pending_targets[0])
                await self._apply_translation(tweet, mode, str(target_session))
            output_path, owned = await self._render(tweet)
            try:
                for target in pending_targets:
                    await self._send_image(target, output_path)
                    key = edge_key(account_id, str(target["id"]))
                    await self._set_cursor(key, tweet.id)
                    edge_cursors[str(target["id"])] = tweet.id
            finally:
                if owned:
                    with suppress(OSError):
                        output_path.unlink()

    async def _apply_translation(self, tweet: Tweet, mode: str, umo: str) -> None:
        targets = _collect_tweets(tweet)
        texts = [item.text.strip() for item in targets if item.text.strip()]
        if not texts:
            return
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=json.dumps(texts, ensure_ascii=False),
                system_prompt=(
                    "把用户给出的 JSON 数组逐项翻译成简体中文。"
                    "保留 URL、@用户名、话题、emoji 和专有名词。只返回 JSON 对象，"
                    '{"translations":["..."]}，数组长度必须与输入相同。'
                ),
            )
        except Exception as exc:
            raise RuntimeError("当前会话没有可用的 LLM provider，无法翻译") from exc
        result_chain = getattr(response, "result_chain", None)
        if result_chain is not None:
            content = result_chain.get_plain_text()
        else:
            content = str(getattr(response, "completion_text", "") or "")
        translations = _parse_translations(content, len(texts))
        index = 0
        for item in targets:
            if not item.text.strip():
                continue
            original = item.text
            translated = translations[index]
            index += 1
            if mode == "translated":
                item.text = translated
                item.translation_text = None
                item.is_translated_text = True
            else:
                item.text = original
                item.translation_text = translated
                item.is_translated_text = False

    async def _render(self, tweet: Tweet) -> tuple[Path, bool]:
        config = BrowserRenderConfig(width=900, timeout_ms=30000)
        backend = str(self.config.get("render_backend") or "astrbot").lower()
        if backend not in {"astrbot", "playwright"}:
            backend = "astrbot"

        async with self._render_lock:
            if backend == "astrbot":
                try:
                    rendered = await self.html_render(
                        render_tweet_html(tweet, config),
                        {},
                        return_url=False,
                        options={
                            "full_page": True,
                            "type": "png",
                            "omit_background": False,
                            "timeout": config.timeout_ms,
                        },
                    )
                    rendered_path = Path(str(rendered))
                    if rendered_path.exists():
                        return rendered_path, False
                except Exception as exc:
                    raise RuntimeError(
                        "AstrBot HTML 渲染失败。服务器部署建议检查 T2I 渲染服务；"
                        "本地电脑可将 render_backend 改为 playwright。"
                    ) from exc

            if self._playwright_renderer is None:
                self._playwright_renderer = AsyncPlaywrightTweetRenderer(config)
            output = self._output_dir / f"{tweet.id}-{uuid.uuid4().hex[:10]}.png"
            try:
                await self._playwright_renderer.render_to_file(tweet, output)
            except Exception as exc:
                raise RuntimeError(
                    "本地 Playwright 渲染失败。请安装 Chromium："
                    "python -m playwright install chromium"
                ) from exc
            return output, True

    async def _send_image(self, target: dict[str, str], path: Path) -> None:
        session = _target_session(target)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"渲染图片不存在或不可读：{path.name}") from exc
        if size <= 0:
            raise RuntimeError("渲染图片为空，无法发送")
        chain = MessageChain().file_image(str(path))
        try:
            sent = await self.context.send_message(session, chain)
        except Exception as exc:
            message = str(exc)
            if "rich media transfer failed" in message.lower():
                raise RuntimeError(
                    "目标平台拒绝了图片上传（rich media transfer failed）。"
                    f"插件已完成取推文和渲染，图片大小约 {size / 1024:.0f} KB；"
                    "请先确认 XFN 能否发送其他本地图片。"
                ) from exc
            raise RuntimeError(
                f"AstrBot 发送图片失败（图片约 {size / 1024:.0f} KB）：{message[:240]}"
            ) from exc
        if sent is False:
            raise RuntimeError(f"AstrBot 找不到推送平台：{target['platform_id']}")

    async def _set_cursor(self, key: str, tweet_id: str) -> None:
        async with self._graph_lock:
            graph = self._store.load()
            graph.setdefault("cursors", {})[key] = tweet_id
            self._store.save(graph)

    async def _load_graph(self) -> dict[str, object]:
        async with self._graph_lock:
            return self._store.load()

    async def _cleanup_old_outputs(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
        for path in self._output_dir.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _register_web_api(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/graph", self._web_get_graph, ["GET"], "Get monitor graph"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/graph/save",
            self._web_save_graph,
            ["POST"],
            "Save monitor graph",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/status", self._web_status, ["GET"], "Get monitor status"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/check", self._web_check, ["POST"], "Run monitor check"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/test-target",
            self._web_test_target,
            ["POST"],
            "Test a push target",
        )

    async def _web_get_graph(self):
        graph = await self._load_graph()
        graph.pop("cursors", None)
        return json_response(graph)

    async def _web_save_graph(self):
        payload = await request.json(default={})
        try:
            normalized = normalize_graph(payload)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        async with self._graph_lock:
            old = self._store.load()
            old_cursors = old.get("cursors", {})
            active_edges = {
                edge_key(item["account_id"], item["target_id"])
                for item in normalized["bindings"]
            }
            old_accounts = {
                str(item["id"]): str(item["username"])
                for item in old.get("accounts", [])
            }
            renamed_accounts = {
                str(item["id"])
                for item in normalized["accounts"]
                if old_accounts.get(str(item["id"]))
                and old_accounts[str(item["id"])] != str(item["username"])
            }
            normalized["cursors"] = {
                key: value
                for key, value in old_cursors.items()
                if key in active_edges
                and key.split(":", 1)[0] not in renamed_accounts
            }
            self._store.save(normalized)
        normalized.pop("cursors", None)
        return json_response({"saved": True, **normalized})

    async def _web_status(self):
        return json_response(self._status_payload())

    async def _web_check(self):
        operation_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        self.logger.info("XMonitor web check started op=%s source=web_check", operation_id)
        ran = await self._run_monitor_cycle()
        elapsed = time.perf_counter() - started
        status = self._status_payload()
        self.logger.info(
            "XMonitor web check finished op=%s source=web_check elapsed=%.2fs errors=%s",
            operation_id,
            elapsed,
            len(status["errors"]),
        )
        return json_response(
            {
                **status,
                "source": "web_check",
                "operation_id": operation_id,
                "elapsed_seconds": round(elapsed, 2),
                "check_started": ran,
                "message": (
                    "立即检查完成，后台状态已刷新；如有新推文会发送到已连线目标。"
                    if ran
                    else "本次检查未启动：已有后台检查正在进行，当前状态已刷新。"
                ),
            }
        )

    async def _web_test_target(self):
        payload = await request.json(default={})
        operation_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        self.logger.info("XMonitor web test started op=%s source=web_test", operation_id)
        try:
            graph = normalize_graph(
                {"accounts": [], "targets": [payload], "bindings": []}
            )
            target = graph["targets"][0]
            output_path, owned = await self._render(sample_tweet())
            try:
                await self._send_image(target, output_path)
            finally:
                if owned:
                    with suppress(OSError):
                        output_path.unlink()
        except (ValueError, RuntimeError) as exc:
            self.logger.warning(
                "XMonitor web test failed op=%s elapsed=%.2fs: %s",
                operation_id,
                time.perf_counter() - started,
                str(exc)[:300],
            )
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            self.logger.exception(
                "XMonitor web test failed op=%s elapsed=%.2fs",
                operation_id,
                time.perf_counter() - started,
            )
            return error_response(
                f"AstrBot 图片发送失败：{str(exc)[:300]}", status_code=502
            )
        elapsed = time.perf_counter() - started
        self.logger.info(
            "XMonitor web test sent op=%s source=web_test elapsed=%.2fs",
            operation_id,
            elapsed,
        )
        return json_response(
            {
                "sent": True,
                "source": "web_test",
                "operation_id": operation_id,
                "elapsed_seconds": round(elapsed, 2),
                "message": "示例测试图片已发送",
            }
        )

    def _status_payload(self) -> dict[str, object]:
        graph = self._store.load()
        return {
            "api_configured": self._api is not None,
            "monitor_running": self._monitor_task is not None
            and not self._monitor_task.done(),
            "last_run": self._last_run,
            "accounts": len(graph["accounts"]),
            "targets": len(graph["targets"]),
            "bindings": len(graph["bindings"]),
            "errors": dict(self._last_errors),
            "render_backend": self.config.get("render_backend", "astrbot"),
        }

    def _translation_mode(self) -> str:
        mode = str(self.config.get("translation_mode") or "none").lower()
        return mode if mode in TRANSLATION_MODES else "none"

    def _extract_argument(self, raw_command: str, injected: str) -> str:
        parts = raw_command.split(None, 1)
        return parts[1].strip() if len(parts) == 2 else injected.strip()

    def _int_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))


def _target_session(target: dict[str, str]) -> MessageSession:
    return MessageSession(
        target["platform_id"],
        MessageType(target["message_type"]),
        target["session_id"],
    )


def _validate_username(username: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        raise ValueError("用户名格式无效")


def _extract_tweet_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return value
    parsed = urlparse(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError("只支持 x.com 或 twitter.com 的推文链接")
    match = re.search(r"/status(?:es)?/(\d+)", parsed.path)
    if not match:
        raise ValueError("链接中没有找到推文 ID")
    return match.group(1)


def _split_translation_mode(value: str) -> tuple[str | None, str]:
    parts = value.split()
    if parts and parts[-1].lower() in TRANSLATION_MODES:
        return parts[-1].lower(), " ".join(parts[:-1]).strip()
    return None, value.strip()


def _collect_tweets(tweet: Tweet) -> list[Tweet]:
    result: list[Tweet] = []
    seen: set[str] = set()

    def visit(current: Tweet) -> None:
        if current.id in seen:
            return
        seen.add(current.id)
        result.append(current)
        for reference in current.references:
            visit(reference.tweet)

    visit(tweet)
    return result


def _parse_translations(content: str, expected: int) -> list[str]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError("LLM 返回的翻译不是有效 JSON") from exc
    translations = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(translations, list) or len(translations) != expected:
        raise RuntimeError("LLM 返回的翻译数量不匹配")
    result = [str(item).strip() for item in translations]
    if not all(result):
        raise RuntimeError("LLM 返回了空翻译")
    return result


def _minimum_cursor(values) -> str | None:
    numeric = [value for value in values if str(value).isdigit()]
    return min(numeric, key=int) if numeric else None


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_text(event: AstrMessageEvent) -> str:
    return (
        "当前 AstrBot 会话信息：\n"
        f"platform_id: {event.get_platform_id()}\n"
        f"message_type: {event.get_message_type().value}\n"
        f"session_id: {event.unified_msg_origin.split(':', 2)[-1]}\n"
        f"unified_msg_origin: {event.unified_msg_origin}"
    )


def _status_text(status: dict[str, object]) -> str:
    errors = status.get("errors") or {}
    error_text = "无" if not errors else json.dumps(errors, ensure_ascii=False)
    return (
        f"监控运行：{'是' if status['monitor_running'] else '否'}\n"
        f"Token：{'已配置' if status['api_configured'] else '未配置'}\n"
        f"账号：{status['accounts']}，目标：{status['targets']}，连线：{status['bindings']}\n"
        f"上次检查：{status['last_run'] or '尚未检查'}\n"
        f"错误：{error_text}"
    )


def _help_text() -> str:
    return (
        "XMonitor 用法：\n"
        "/xmonitor <X 推文链接或 ID>\n"
        "/xmonitor latest @用户名\n"
        "/xmonitor sample  （本地示例，不需要 Token）\n"
        "/xmonitor where   （显示当前会话目标信息）\n"
        "/xmonitor status\n"
        "/xmonitor check\n"
        "在命令末尾添加 none、translated 或 bilingual 可临时切换翻译模式。"
    )
