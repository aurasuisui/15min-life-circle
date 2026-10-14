"""client.py 单测：配置读取 / 重试 / 超时 / 异常一律不外抛（零网络）。"""
from __future__ import annotations

import httpx
import pytest

from app.ai.client import (
    VALID_EFFORTS,
    ChatResult,
    DeepSeekClient,
    LLMConfig,
    load_dotenv,
    parse_json_object,
)

TICK = chr(96) * 3  # markdown 代码块围栏


# ---------------- 配置 ----------------
def test_config_defaults_without_env(llm_env):
    llm_env()
    cfg = LLMConfig.from_env()
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-flash"
    assert cfg.effort == "low", "默认必须用 low 控制延迟与成本"
    assert cfg.max_tokens == 4000, "推理模型必须留足 max_tokens"
    assert cfg.timeout_s >= 20, "香港出口延迟高，超时必须 >= 20s（契约要求）"
    assert cfg.configured


def test_config_reads_all_env(llm_env):
    llm_env(LLM_BASE_URL="https://example.com/v1/", LLM_MODEL="deepseek-v4-pro",
            LLM_EFFORT="high", LLM_TIMEOUT_S="60", LLM_MAX_TOKENS="8000", LLM_MAX_RETRIES="2")
    cfg = LLMConfig.from_env()
    assert cfg.base_url == "https://example.com/v1"   # 去掉尾斜杠
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.effort == "high"
    assert cfg.timeout_s == 60
    assert cfg.max_tokens == 8000
    assert cfg.max_retries == 2


def test_config_rejects_invalid_effort(llm_env):
    llm_env(LLM_EFFORT="ultra")
    assert LLMConfig.from_env().effort == "low"


def test_config_not_configured_without_key(llm_env):
    llm_env(LLM_API_KEY="")
    assert not LLMConfig.from_env().configured


def test_config_safe_dict_masks_key(llm_env):
    llm_env(LLM_API_KEY="sk-super-secret-value")
    d = LLMConfig.from_env().safe_dict()
    assert "api_key" not in d
    assert d["has_api_key"] is True
    assert "sk-super-secret-value" not in repr(d)


def test_load_dotenv_reads_repo_file():
    """真实 .env 存在时应能解析出 LLM_MODEL（只断言键名，绝不打印值）。"""
    vals = load_dotenv(force=True)
    if vals:
        assert "LLM_MODEL" in vals
        assert "LLM_API_KEY" in vals


# ---------------- JSON 解析 ----------------
def test_parse_json_object_plain():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_object_with_padding():
    assert parse_json_object('   {"a": 1}  ') == {"a": 1}


def test_parse_json_object_fenced():
    text = TICK + "json\n" + '{"a": 1}' + "\n" + TICK
    assert parse_json_object(text) == {"a": 1}


def test_parse_json_object_with_prose():
    text = "好的，结果如下：\n" + '{"a": 1}' + "\n以上。"
    assert parse_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "   ", "not json", "[1,2,3]"])
def test_parse_json_object_returns_none(text):
    assert parse_json_object(text) is None


# ---------------- 请求与容错 ----------------
def _patch_httpx(monkeypatch, handler):
    """把 httpx.Client 换成走 MockTransport 的实例（零真实网络）。"""
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)


def _ok_body(content: str = '{"diagnosis":"ok"}') -> dict:
    return {
        "id": "x", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content,
                                 "reasoning_content": "思考过程" * 10}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                  "completion_tokens_details": {"reasoning_tokens": 15}},
    }


def test_successful_call_and_reasoning_captured(monkeypatch, llm_env):
    llm_env()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(request.content.decode("utf-8"))
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_ok_body())

    _patch_httpx(monkeypatch, handler)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])

    assert res.ok and res.content == '{"diagnosis":"ok"}'
    assert res.reasoning_content, "必须能取到 reasoning_content"
    assert res.usage["completion_tokens_details"]["reasoning_tokens"] == 15
    assert res.attempts == 1
    assert seen["auth"] == "Bearer sk-test-not-a-real-key"


def test_payload_carries_effort_and_max_tokens(monkeypatch, llm_env):
    llm_env(LLM_EFFORT="low", LLM_MAX_TOKENS="4000")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_body())

    _patch_httpx(monkeypatch, handler)
    DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])
    assert seen["body"]["effort"] == "low"
    assert seen["body"]["max_tokens"] == 4000
    assert seen["body"]["model"] == "deepseek-flash"


def test_effort_override_per_call(monkeypatch, llm_env):
    llm_env()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_body())

    _patch_httpx(monkeypatch, handler)
    DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}], effort="max")
    assert seen["body"]["effort"] == "max"
    assert "max" in VALID_EFFORTS


def test_not_configured_returns_error_without_request(monkeypatch, llm_env):
    llm_env(LLM_API_KEY="")
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json=_ok_body())

    _patch_httpx(monkeypatch, handler)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])
    assert not res.ok
    assert "LLM 未配置" in res.error
    assert called["n"] == 0


def test_timeout_retries_then_fails_soft(monkeypatch, llm_env):
    llm_env(LLM_MAX_RETRIES="2", LLM_TIMEOUT_S="5")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("too slow", request=request)

    _patch_httpx(monkeypatch, handler)
    monkeypatch.setattr("app.ai.client.time.sleep", lambda *_: None)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])

    assert not res.ok
    assert calls["n"] == 3, "首次 + 最多 2 次重试"
    assert res.attempts == 3
    assert "超时" in res.error


def test_server_error_retries_then_succeeds(monkeypatch, llm_env):
    llm_env(LLM_MAX_RETRIES="2")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_ok_body())

    _patch_httpx(monkeypatch, handler)
    monkeypatch.setattr("app.ai.client.time.sleep", lambda *_: None)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])
    assert res.ok and calls["n"] == 3


def test_truncated_json_identified_as_length_limit(monkeypatch, llm_env):
    """推理+正文吃满 max_tokens -> JSON 被截断，必须报"截断"而不是"不是合法 JSON"。"""
    llm_env(LLM_MAX_RETRIES="0")

    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body('{"diagnosis": "本社区综合评分 59.75 分，等级 C，等时圈面积分别')
        body["choices"][0]["finish_reason"] = "length"
        body["usage"]["completion_tokens"] = 4000
        body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2800}
        return httpx.Response(200, json=body)

    _patch_httpx(monkeypatch, handler)
    obj, res = DeepSeekClient(LLMConfig.from_env()).chat_json([{"role": "user", "content": "hi"}])
    assert obj is None and not res.ok
    assert "截断" in res.error and "max_tokens" in res.error
    assert res.usage["completion_tokens"] == 4000, "截断时应保留 usage 便于排障"
    assert "不是合法 JSON" not in res.error, "不要给出误导性的解析错误"


def test_auth_error_fails_fast_without_retry(monkeypatch, llm_env):
    llm_env(LLM_MAX_RETRIES="2")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    _patch_httpx(monkeypatch, handler)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])
    assert not res.ok
    assert calls["n"] == 1, "鉴权错误不可重试"
    assert "401" in res.error


def test_empty_content_identified_as_token_starvation(monkeypatch, llm_env):
    """推理吃满 max_tokens 时 content 为空——必须给出可诊断的错误。"""
    llm_env(LLM_MAX_RETRIES="0")

    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body("")
        body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=body)

    _patch_httpx(monkeypatch, handler)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])
    assert not res.ok
    assert "max_tokens" in res.error


def test_invalid_json_content(monkeypatch, llm_env):
    llm_env(LLM_MAX_RETRIES="0")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("这不是 JSON"))

    _patch_httpx(monkeypatch, handler)
    obj, res = DeepSeekClient(LLMConfig.from_env()).chat_json([{"role": "user", "content": "hi"}])
    assert obj is None
    assert not res.ok
    assert "不是合法 JSON" in res.error


def test_connection_error_never_raises(monkeypatch, llm_env):
    llm_env(LLM_MAX_RETRIES="0")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail", request=request)

    _patch_httpx(monkeypatch, handler)
    res = DeepSeekClient(LLMConfig.from_env()).chat([{"role": "user", "content": "hi"}])
    assert not res.ok and "网络错误" in res.error


def test_endpoint_normalization():
    c = DeepSeekClient(LLMConfig(base_url="https://api.deepseek.com/", api_key="k"))
    assert c.endpoint == "https://api.deepseek.com/chat/completions"
    c2 = DeepSeekClient(LLMConfig(base_url="https://api.deepseek.com/chat/completions", api_key="k"))
    assert c2.endpoint == "https://api.deepseek.com/chat/completions"


def test_health_does_not_leak_key(llm_env):
    llm_env(LLM_API_KEY="sk-secret-xyz")
    h = DeepSeekClient(LLMConfig.from_env()).health()
    assert h["llm"] is True and h["has_api_key"] is True
    assert "sk-secret-xyz" not in repr(h)


def test_chat_result_dict_shape():
    r = ChatResult(ok=True, content="x", model="deepseek-flash")
    d = r.as_dict()
    assert d["ok"] is True and "usage" in d and d["content_chars"] == 1
