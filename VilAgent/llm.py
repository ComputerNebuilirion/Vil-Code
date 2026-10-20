"""LLM 客户端：httpx async，OpenAI-compatible API"""
import asyncio
import email.utils
import json
import random
import time
from typing import AsyncGenerator, Callable

from .i18n import t

# 软依赖：httpx 只在真正调用 LLM 时才必需。
# 否则 `import VilAgent`（连带 tests 里的 safety/tools 用例）在干净环境下会
# 直接 ModuleNotFoundError，Linux 部署/CI 里很别扭。
try:
    import httpx
except ImportError:  # pragma: no cover - 取决于环境是否装了 httpx
    httpx = None  # type: ignore[assignment]


class LLMError(Exception):
    pass


def _require_httpx():
    """延迟校验软依赖：只在要发请求时报错，import 期不炸"""
    if httpx is None:
        raise LLMError(t(
            "缺少依赖 httpx，请先安装：pip install httpx",
            "missing dependency httpx; install it with: pip install httpx",
        ))
    return httpx


# 触发重试的 HTTP 状态码（临时性故障）
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMClient:
    """
    OpenAI-compatible LLM 客户端

    支持 OpenAI / Ollama / vLLM / LM Studio 等，只要兼容 OpenAI chat completions API。
    """

    def __init__(self, endpoint: str, model: str,
                 api_key: str | None = None,
                 timeout: float = 120,
                 temperature: float = 0.2,
                 max_tokens: int | None = None,
                 max_retries: int = 3,
                 retry_base_delay: float = 0.5):
        _require_httpx()  # 缺 httpx 时在这里给出友好报错，而不是 import 期就崩
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        # 最近一次 chat 调用的统计（调用后可读）
        self.last_usage: dict | None = None
        self.last_latency: float | None = None

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.AsyncClient(
            base_url=self.endpoint,
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self):
        await self._client.aclose()

    def recreate_client(self):
        """重建 httpx AsyncClient（跨 asyncio.run() 事件循环时使用）"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.endpoint,
            headers=headers,
            timeout=self.timeout,
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        on_chunk: Callable | None = None,
    ) -> dict:
        """
        调用 chat completions API（含重试/退避 + 流式降级）

        重试策略：
        - 可重试错误：429/5xx/408/连接超时/读超时/TransportError
        - 退避：指数退避 base * 2^attempt + jitter，上限 30s；
          若响应带 Retry-After 头则尊重之
        - 流式降级：若流式请求在中途失败（已通过 on_chunk 发出内容），
          后续重试改走非流式（避免 UI 重复文本）；非流式成功后把
          "已发送内容之后"的剩余部分经 on_chunk 补发，保证 view 的
          累积缓冲最终等于完整内容

        :return: assistant message dict
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": stream,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream_options"] = {"include_usage": True}

        self.last_usage = None
        self.last_latency = None
        t0 = time.perf_counter()

        # 流式降级追踪：记录已通过 on_chunk 发出的 content
        emitted = {"content": ""}
        # 重试中可能从流式切到非流式（降级），用本地变量控制
        cur_stream = stream

        last_exc: Exception | None = None
        last_retry_after: float | None = None

        for attempt in range(self.max_retries + 1):
            try:
                if cur_stream:
                    result = await self._chat_stream(payload, on_chunk, emitted)
                else:
                    result = await self._chat_normal(payload)
                    # 降级模式：非流式重试成功后，补发未发送的剩余 content。
                    # 流式已发了 emitted["content"] 前缀，这里补齐后续，
                    # view 的 _content_buf 累积后即为完整内容，不会重复。
                    if stream and on_chunk and emitted["content"]:
                        full = result.get("content") or ""
                        remaining = full[len(emitted["content"]):]
                        if remaining:
                            await on_chunk({"type": "content", "chunk": remaining})
                self.last_latency = time.perf_counter() - t0
                return result

            except httpx.HTTPStatusError as e:
                last_exc = e
                code = e.response.status_code
                if code not in _RETRYABLE_STATUS:
                    # 非临时性错误（如 400/401/404），不重试直接抛
                    raise LLMError(f"HTTP {code}: {e.response.text}") from e
                last_retry_after = self._parse_retry_after(
                    e.response.headers.get("Retry-After"))
            except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                last_exc = e
                last_retry_after = None
            except httpx.TransportError as e:
                # 其他传输层错误（连接重置、RemoteProtocolError 等）也重试
                last_exc = e
                last_retry_after = None

            # 末次尝试失败 → 不再退避，跳出后抛汇总错误
            if attempt == self.max_retries:
                break

            # 退避等待
            delay = (last_retry_after if last_retry_after is not None
                     else self._compute_backoff(attempt))
            await asyncio.sleep(delay)

            # 流式且已发出 chunk → 降级为非流式，避免重试导致 UI 重复
            if cur_stream and emitted["content"]:
                cur_stream = False
                payload["stream"] = False
                payload.pop("stream_options", None)

        # 重试耗尽：抛出带重试次数的汇总错误
        n = self.max_retries
        if isinstance(last_exc, httpx.HTTPStatusError):
            body = (last_exc.response.text or "")[:200]
            raise LLMError(
                f"HTTP {last_exc.response.status_code} "
                f"(after {n} retries): {body}") from last_exc
        if isinstance(last_exc, httpx.ConnectTimeout):
            raise LLMError(f"connection timeout (after {n} retries)") from last_exc
        if isinstance(last_exc, httpx.ReadTimeout):
            raise LLMError(
                f"read timeout (after {n} retries, LLM took too long)") from last_exc
        raise LLMError(
            f"transport error (after {n} retries): {last_exc}") from last_exc

    async def _chat_normal(self, payload: dict) -> dict:
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        self.last_usage = data.get("usage")
        return data["choices"][0]["message"]

    async def _chat_stream(
        self, payload: dict, on_chunk: Callable | None,
        emitted: dict | None = None,
    ) -> dict:
        """流式：累积 tool_calls 分片，content delta 经 on_chunk 转发。

        emitted: 降级追踪容器（{"content": str}），记录已发出的 content，
        供 chat() 流式降级为非流式重试时计算需补发的剩余部分。
        """
        content_buf: list[str] = []
        # tool_calls 按 index 累积
        acc: dict[int, dict] = {}

        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                # stream 模式必须先 aread 才能访问 body，否则抛
                # "Attempted to access streaming response content, without having called read()"
                await resp.aread()
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request, response=resp,
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break

                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # 流式最终 chunk 携带 usage
                if obj.get("usage"):
                    self.last_usage = obj["usage"]

                choices = obj.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                # content delta
                if delta.get("content"):
                    chunk = delta["content"]
                    content_buf.append(chunk)
                    if on_chunk:
                        await on_chunk({"type": "content", "chunk": chunk})
                    if emitted is not None:
                        emitted["content"] += chunk

                # tool_calls delta
                for tc in delta.get("tool_calls", []):
                    idx = tc.get("index", 0)
                    slot = acc.setdefault(idx, {
                        "id": "", "name": "", "arguments_buf": []
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments_buf"].append(fn["arguments"])
                    if on_chunk:
                        await on_chunk({
                            "type": "tool_call_delta",
                            "index": idx,
                            "name": slot["name"],
                        })

        # 拼装最终 assistant message
        tool_calls = None
        if acc:
            tool_calls = [
                {
                    "id": s["id"],
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "arguments": "".join(s["arguments_buf"]),
                    }
                }
                for _, s in sorted(acc.items())
            ]

        content = "".join(content_buf) if content_buf else None

        return {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }

    # —— 重试 / 退避辅助 ——

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        """解析 Retry-After 头：可能是秒数（"120"）或 HTTP 日期。

        返回等待秒数；无法解析返回 None（由调用方走指数退避）。
        """
        if not value:
            return None
        value = value.strip()
        # 形如 "120" 的纯数字 → 秒数
        if value.isdigit():
            secs = int(value)
            return float(min(secs, 60))  # 上限 60s 避免过长阻塞
        # 形如 "Wed, 21 Oct 2026 07:28:00 GMT" 的 HTTP 日期
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt is None:
                return None
            secs = (dt.timestamp() - time.time())
            return float(max(0, min(secs, 60)))
        except (TypeError, ValueError):
            return None

    def _compute_backoff(self, attempt: int) -> float:
        """指数退避 + 抖动：base * 2^attempt + 0~1 随机抖动，上限 30s。

        attempt 从 0 开始（第一次重试 attempt=0 → base*1）。
        """
        delay = self.retry_base_delay * (2 ** attempt)
        delay = min(delay, 30.0)
        delay += random.uniform(0, 1)  # jitter 避免同步重试惊群
        return delay
