"""DeepSeek 客户端（OpenAI 兼容协议）—— S4 交付物 1/6。

- 模型：deepseek-flash（DeepSeek-V4.1-Flash）。**它是推理模型**：
  响应 message 里带 reasoning_content 字段，且支持顶层 effort 参数（low / high / max）。
  本项目默认 LLM_EFFORT=low，以控制延迟与成本（高 effort 会显著拉长思考链）。
- 推理过程**先消耗 token**，因此 max_tokens 必须留足（默认 4000，见 .env LLM_MAX_TOKENS）；
  max_tokens 过小会出现「reasoning 吃满、content 为空」的失败形态，本模块会显式识别该情况。
- **超时、重试（最多 2 次）、异常一律不外抛**：统一收敛为 ChatResult(ok=False)，
  由上层 generate_report 转成模板降级，保证 API 永不 500。
- 只用 httpx（已安装），不引入 openai SDK，避免额外依赖与隐式重试语义。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx

__all__ = [
    "ChatResult",
    "LLMConfig",
    "DeepSeekClient",
    "VALID_EFFORTS",
    "get_client",
    "load_dotenv",
    "chat_json",
    "parse_json_object",
]

#: 允许的推理强度（来自 DeepSeek /models 的 effort.supported_levels）
VALID_EFFORTS: tuple[str, ...] = ("low", "high", "max")

_ENV_FILE_CACHE: dict[str, str] | None = None
_DEFAULT_MAX_TOKENS = 4000
_DEFAULT_TIMEOUT_S = 40.0
_MAX_ATTEMPTS = 3  # 首次 + 最多 2 次重试


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _find_dotenv() -> Path | None:
    """从包所在位置向上查找 .env（backend/app/ai -> ... -> 仓库根）。"""
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        cand = parent / ".env"
        if cand.is_file():
            return cand
    return None


def load_dotenv(force: bool = False) -> dict[str, str]:
    """读取仓库根目录 .env（不覆盖已存在的真实环境变量）。

    仅用于本地开发兜底：pydantic-settings 只作用于 app.config.Settings，
    而 AI 层要保持可独立运行（可被测试与脚本直接 import），故做一次轻量解析。
    解析结果只驻留内存，绝不写回磁盘、绝不打日志。
    """
    global _ENV_FILE_CACHE
    if _ENV_FILE_CACHE is not None and not force:
        return _ENV_FILE_CACHE

    values: dict[str, str] = {}
    path = _find_dotenv()
    if path is not None:
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                    val = val[1:-1]
                if key and " " not in key:
                    values[key] = val
        except OSError:
            values = {}
    _ENV_FILE_CACHE = values
    return values


def _env(name: str, default: str = "") -> str:
    """优先真实环境变量，其次 .env 文件，最后默认值。"""
    v = os.environ.get(name)
    if v is not None and v != "":
        return v
    v = load_dotenv().get(name)
    if v is not None and v != "":
        return v
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


@dataclass
class LLMConfig:
    """LLM 运行配置（全部来自环境变量）。"""

    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-flash"
    effort: str = "low"
    timeout_s: float = _DEFAULT_TIMEOUT_S
    max_tokens: int = _DEFAULT_MAX_TOKENS
    max_retries: int = 2
    temperature: float = 0.2
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "LLMConfig":
        effort = (_env("LLM_EFFORT", "low") or "low").strip().lower()
        if effort not in VALID_EFFORTS:
            effort = "low"
        return cls(
            base_url=(_env("LLM_BASE_URL", "https://api.deepseek.com") or "").rstrip("/"),
            api_key=_env("LLM_API_KEY", ""),
            model=_env("LLM_MODEL", "deepseek-flash") or "deepseek-flash",
            effort=effort,
            timeout_s=max(_env_float("LLM_TIMEOUT_S", _DEFAULT_TIMEOUT_S), 5.0),
            max_tokens=max(_env_int("LLM_MAX_TOKENS", _DEFAULT_MAX_TOKENS), 512),
            max_retries=max(_env_int("LLM_MAX_RETRIES", 2), 0),
            temperature=_env_float("LLM_TEMPERATURE", 0.2),
            enabled=_env_bool("LLM_ENABLED", True),
        )

    @property
    def configured(self) -> bool:
        """是否具备真实可用的调用条件。"""
        return bool(self.api_key) and bool(self.base_url) and bool(self.model)

    def safe_dict(self) -> dict[str, Any]:
        """**脱敏**后的配置摘要（可安全写日志/报告，绝不含 API Key）。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "effort": self.effort,
            "timeout_s": self.timeout_s,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            "enabled": self.enabled,
            "has_api_key": bool(self.api_key),
        }


@dataclass
class ChatResult:
    """一次 LLM 调用的结果（**永不抛异常**）。"""

    ok: bool = False
    content: str = ""
    reasoning_content: str = ""
    error: str = ""
    status_code: int | None = None
    attempts: int = 0
    elapsed_s: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
            "model": self.model or None,
            "usage": self.usage,
            "reasoning_chars": len(self.reasoning_content or ""),
            "content_chars": len(self.content or ""),
        }

    def json_object(self) -> dict[str, Any] | None:
        """把 content 解析成 JSON 对象（容忍 markdown 代码块包裹）。"""
        return parse_json_object(self.content)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """宽松 JSON 解析：容忍代码块包裹、前后缀说明文字。"""
    if not text or not text.strip():
        return None
    s = text.strip()
    if s.startswith("```"):
        chunks = s.split("```")
        if len(chunks) > 1:
            s = chunks[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(s[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
#: 可重试的 HTTP 状态码（瞬时错误）
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})


class DeepSeekClient:
    """DeepSeek（OpenAI 兼容）同步客户端。

    用法::

        client = DeepSeekClient(LLMConfig.from_env())
        res = client.chat(messages, response_format={"type": "json_object"})
        if res.ok:
            data = res.json_object()
    """

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()

    # -- 内部工具 ---------------------------------------------------------
    @property
    def endpoint(self) -> str:
        base = (self.config.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: Sequence[dict[str, str]],
        *,
        response_format: dict[str, Any] | None,
        max_tokens: int | None,
        temperature: float | None,
        effort: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            # 推理模型：reasoning_content 会先消耗 token，max_tokens 必须留足
            "max_tokens": int(max_tokens or self.config.max_tokens),
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": False,
        }
        chosen = (effort or self.config.effort or "low").strip().lower()
        if chosen in VALID_EFFORTS:
            # effort 是 DeepSeek-V4.1 系列的顶层参数（low | high | max）
            payload["effort"] = chosen
        if response_format is not None:
            payload["response_format"] = response_format
        return payload

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        effort: str | None = None,
    ) -> ChatResult:
        """发起一次对话补全。**任何异常都不外抛**，统一收敛到 ChatResult。"""
        cfg = self.config
        res = ChatResult(model=cfg.model)
        if not cfg.configured:
            res.error = "LLM 未配置（缺少 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL）"
            return res

        payload = self._payload(
            messages,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            effort=effort,
        )
        attempts = min(int(cfg.max_retries) + 1, _MAX_ATTEMPTS)
        started = time.monotonic()

        for i in range(attempts):
            res.attempts = i + 1
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(cfg.timeout_s, connect=min(15.0, cfg.timeout_s)),
                    follow_redirects=True,
                    trust_env=True,  # 允许走系统代理（本机网络出口在香港）
                ) as http:
                    resp = http.post(self.endpoint, headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                res.error = f"超时（{cfg.timeout_s:g}s 未返回）：{type(exc).__name__}"
                if i < attempts - 1:
                    time.sleep(1.5 * (i + 1))
                    continue
                break
            except httpx.HTTPError as exc:  # 连接 / 代理 / 协议错误
                res.error = f"网络错误：{type(exc).__name__}: {exc}"
                if i < attempts - 1:
                    time.sleep(1.5 * (i + 1))
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让调用方接到异常
                res.error = f"未知异常：{type(exc).__name__}: {exc}"
                break

            res.status_code = resp.status_code
            if resp.status_code in _RETRYABLE_STATUS:
                res.error = f"HTTP {resp.status_code}（可重试）：{_brief(resp.text)}"
                if i < attempts - 1:
                    time.sleep(1.5 * (i + 1))
                    continue
                break
            if resp.status_code >= 400:
                # 400/401/403/404 等鉴权或参数错误：快速失败，不重试（避免无谓延迟）
                res.error = f"HTTP {resp.status_code}（不可重试）：{_brief(resp.text)}"
                break

            try:
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                res.error = f"响应非 JSON：{type(exc).__name__}"
                break

            if isinstance(body, dict) and body.get("error"):
                res.error = f"接口返回错误：{_brief(json.dumps(body['error'], ensure_ascii=False))}"
                res.raw = body
                break

            choices = (body or {}).get("choices") or []
            if not choices:
                res.error = f"响应缺少 choices：{_brief(resp.text)}"
                res.raw = body if isinstance(body, dict) else None
                break

            finish = (choices[0] or {}).get("finish_reason")
            message = (choices[0] or {}).get("message") or {}
            content = message.get("content") or ""

            # 推理模型（deepseek-flash）最常见的两种"预算不足"失败，必须单独识别：
            # ① 推理吃满 max_tokens，content 直接为空；
            # ② reasoning + 正文合计吃满 max_tokens，JSON 被**截断**在半句话上。
            #    后者若直接交给 JSON 解析器，会走到"首尾大括号"兜底逻辑并抛出
            #    误导性的「输出不是合法 JSON」，掩盖真实的 token 预算不足问题。
            if finish == "length":
                res.usage = (body or {}).get("usage") or {}
                detail = (res.usage or {}).get("completion_tokens_details") or {}
                res.error = (
                    "输出被 max_tokens 截断（finish_reason=length，"
                    f"completion_tokens={(res.usage or {}).get('completion_tokens')}，"
                    f"reasoning_tokens={detail.get('reasoning_tokens')}，"
                    f"content_chars={len(content)}）"
                )
                res.raw = body if isinstance(body, dict) else None
                break

            if not isinstance(content, str) or not content.strip():
                res.error = f"响应 content 为空（finish_reason={finish}，可能是 max_tokens 不足）"
                res.raw = body if isinstance(body, dict) else None
                break

            res.ok = True
            res.content = content
            res.reasoning_content = message.get("reasoning_content") or ""
            res.usage = (body or {}).get("usage") or {}
            res.model = (body or {}).get("model") or cfg.model
            if isinstance(body, dict):
                res.raw = body
            break

        res.elapsed_s = time.monotonic() - started
        if not res.ok and not res.error:
            res.error = "未知失败"
        return res

    def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, ChatResult]:
        """要求 JSON 对象输出并解析。返回 (对象或 None, ChatResult)。"""
        kwargs.setdefault("response_format", {"type": "json_object"})
        res = self.chat(messages, **kwargs)
        if not res.ok:
            return None, res
        obj = res.json_object()
        if obj is None:
            res.ok = False
            res.error = f"输出不是合法 JSON：{_brief(res.content)}"
            return None, res
        return obj, res

    def health(self) -> dict[str, Any]:
        """轻量可用性探测（仅检查配置，不发请求，供 /api/health 使用）。"""
        cfg = self.config
        return {
            "llm": bool(cfg.enabled and cfg.configured),
            "model": cfg.model,
            "effort": cfg.effort,
            "has_api_key": bool(cfg.api_key),
        }


def _brief(text: str, limit: int = 200) -> str:
    s = (text or "").replace("\n", " ").strip()
    return s[:limit] + ("…" if len(s) > limit else "")


# --------------------------------------------------------------------------
# 模块级便捷函数
# --------------------------------------------------------------------------
_CLIENT: DeepSeekClient | None = None


def get_client(config: LLMConfig | None = None, *, refresh: bool = False) -> DeepSeekClient:
    """取全局客户端（进程内缓存，避免重复建连）。"""
    global _CLIENT
    if _CLIENT is None or refresh or config is not None:
        _CLIENT = DeepSeekClient(config)
    return _CLIENT


def chat_json(
    messages: Sequence[dict[str, str]],
    *,
    config: LLMConfig | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any] | None, ChatResult]:
    """一行式：构建/复用客户端并请求 JSON 输出。"""
    return get_client(config).chat_json(messages, **kwargs)
