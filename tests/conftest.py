import pytest

from assistant import tools


@pytest.fixture(autouse=True)
def keep_the_real_audit_log_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "AUDIT_LOG", tmp_path / "audit.log")
