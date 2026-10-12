"""配置自检（probe）与错误透传的单元测试。

覆盖真实踩过的坑：apiBase 填成官网地址 → 404 返回 HTML，
而 ValidationError 只暴露「N 个校验问题」，真实原因被丢弃。
"""
from dataclasses import replace
import io
import json
import urllib.error

import pytest

from prd2tc.config import Config
from prd2tc.llm import probe_provider
from prd2tc.pipeline import GenerateRequest, PipelineRunner
from prd2tc.validator import ValidationError, format_exception


def _cfg(**kw) -> Config:
    base = Config(api_key="sk-test", api_base="https://api.example.com/v1", model="m",
                  temperature=0.0, timeout=10, max_retries=1)
    return replace(base, **kw)


class _Response:
    def __init__(self, body: str, status: int = 200, content_type: str = "application/json"):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: str, content_type: str = "application/json") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example.com/v1/chat/completions", code,
                                  "err", {"Content-Type": content_type}, io.BytesIO(body.encode("utf-8")))


def test_probe_ok(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Response(json.dumps({"model": "m-1", "choices": []})))
    probe = probe_provider(_cfg())
    assert probe.ok and probe.kind == "ok"
    assert probe.model == "m-1"
    assert probe.endpoint.endswith("/v1/chat/completions")


def test_probe_html_console_page_is_endpoint_error(monkeypatch):
    """本次真实故障：apiBase 填成官网，404 返回的是控制台网页。"""
    html = "<!DOCTYPE html><html><head><title>MiniMax API 开放平台</title></head></html>"
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(404, html, "text/html")))
    probe = probe_provider(_cfg(api_base="https://platform.minimax.cn"))
    assert not probe.ok
    assert probe.kind == "endpoint"
    assert "网页" in probe.message and "MiniMax API 开放平台" in probe.message
    assert "控制台" in probe.hint


def test_probe_missing_v1_suggests_base(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(404, "Not Found", "text/plain")))
    probe = probe_provider(_cfg(api_base="https://api.minimax.chat"))
    assert probe.suggested_base == "https://api.minimax.chat/v1"
    assert "/v1" in probe.hint


def test_probe_auth_error(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(401, '{"error":"bad key"}')))
    probe = probe_provider(_cfg())
    assert probe.kind == "auth" and probe.http_status == 401
    assert "API Key" in probe.hint


def test_probe_rate_limit_and_model(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(429, "slow down")))
    assert probe_provider(_cfg()).kind == "rate_limit"
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(400, '{"error":"unknown model"}')))
    assert probe_provider(_cfg()).kind == "model"


def test_probe_network_error(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("getaddrinfo failed")))
    probe = probe_provider(_cfg(api_base="https://no-such-host.invalid/v1"))
    assert probe.kind == "network"
    assert "网络" in probe.hint


@pytest.mark.parametrize("api_base,key,needle", [
    ("", "sk-x", "apiBase 为空"),
    ("platform.example.com/v1", "sk-x", "http(s)://"),
    ("https://api.example.com/v1", "", "API Key"),
])
def test_probe_config_prechecks(api_base, key, needle):
    probe = probe_provider(_cfg(api_base=api_base, api_key=key))
    assert probe.kind == "config" and needle in probe.message


def test_probe_never_raises(monkeypatch):
    """探测本身绝不抛出，否则会阻断保存流程。"""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    probe = probe_provider(_cfg())
    assert probe.ok is False and probe.kind == "malformed"


def test_format_exception_keeps_validation_details():
    """校验异常的 str() 只有数量，真实原因必须保留下来。"""
    exc = ValidationError(["需求分段 1/2 失败: HTTP 404: <html>", "需求分段 2/2 失败: HTTP 404: <html>"])
    text = format_exception(exc)
    assert "2 个校验问题" not in text
    assert "HTTP 404" in text and "需求分段 1/2" in text


def test_format_exception_plain_error():
    assert format_exception(ValueError("未配置 LLM 凭证")) == "ValueError: 未配置 LLM 凭证"


def test_pipeline_persists_real_error_and_errors_list(tmp_path, monkeypatch):
    """运行期失败时，manifest 与 result.errors 都要带真实原因。"""
    def boom(cfg, user_text, **kwargs):
        raise ValidationError(["需求分段 1/1 失败: HTTP 404: 官网页面"])
    from prd2tc import stages
    monkeypatch.setattr(stages, "chat_completion", boom)
    prd = tmp_path / "p.md"
    prd.write_text("# PRD\n## 功能需求\nREQ-A-001 登录\n", encoding="utf-8")
    runner = PipelineRunner(_cfg())
    runner.llm = boom
    runner._custom_llm = True
    with pytest.raises(ValidationError):
        runner.generate(GenerateRequest(prd=prd, out=tmp_path / "out", product="p", version="v1"))
    manifest = json.loads((tmp_path / "out" / "p" / "v1" / "run-manifest.json").read_text(encoding="utf-8"))
    assert "HTTP 404" in manifest["error"]
    assert "2 个校验问题" not in manifest["error"]
