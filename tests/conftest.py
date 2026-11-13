import pytest


@pytest.fixture(autouse=True)
def _keep_test_logs_out_of_the_repo(tmp_path, monkeypatch):
    """Fixture audits must not append to the desk's logs/auditor.log."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DESK_AUDIT_CACHE", str(tmp_path / "audit-cache"))
    monkeypatch.setenv("DESK_AUDIT_RETRY", str(tmp_path / "audit-retry.json"))
