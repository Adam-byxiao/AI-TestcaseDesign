"""并发默认值测试：PRD2TC_JOBS 生效，且 CLI/服务不再把它硬编码为 1。"""
from prd2tc.config import Config


def test_config_jobs_from_env(monkeypatch):
    monkeypatch.delenv("PRD2TC_JOBS", raising=False)
    assert Config.from_env().jobs == 1
    monkeypatch.setenv("PRD2TC_JOBS", "4")
    assert Config.from_env().jobs == 4
    monkeypatch.setenv("PRD2TC_JOBS", "0")
    assert Config.from_env().jobs == 1, "非法值应回落到 1，而不是 0（0 会让并发池崩溃）"
    monkeypatch.setenv("PRD2TC_JOBS", "-3")
    assert Config.from_env().jobs == 1


def test_cli_defaults_jobs_from_config(monkeypatch):
    """--jobs 未指定时应取 PRD2TC_JOBS，而不是永远串行。"""
    import argparse
    from prd2tc import cli
    monkeypatch.setenv("PRD2TC_JOBS", "5")
    parser = cli.build_parser()
    args = parser.parse_args(["generate", "--prd", "x.md", "--stub"])
    assert args.jobs is None, "默认必须留空，交由 Config 决定"
    cfg = Config.from_env()
    assert (args.jobs or cfg.jobs) == 5


def test_service_uses_config_jobs_when_body_omits(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from prd2tc.service import create_app
    monkeypatch.setenv("PRD2TC_JOBS", "3")
    app = create_app(state_dir=tmp_path / "ws")
    with TestClient(app) as client:
        # 通过 create_run 的请求体落盘内容验证 jobs 被采用
        sample = tmp_path / "p.md"
        sample.write_text("# PRD\n## 功能需求\nREQ-A-001 登录\n", encoding="utf-8")
        prd = client.post("/api/v1/prds", files={"file": ("p.md", sample.read_bytes(), "text/markdown")}).json()
        run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True}).json()
        assert run["request"]["jobs"] == 3, run["request"]


def test_explicit_jobs_overrides_env(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from prd2tc.service import create_app
    monkeypatch.setenv("PRD2TC_JOBS", "3")
    app = create_app(state_dir=tmp_path / "ws")
    with TestClient(app) as client:
        sample = tmp_path / "p.md"
        sample.write_text("# PRD\n## 功能需求\nREQ-A-001 登录\n", encoding="utf-8")
        prd = client.post("/api/v1/prds", files={"file": ("p.md", sample.read_bytes(), "text/markdown")}).json()
        run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 7}).json()
        assert run["request"]["jobs"] == 7
