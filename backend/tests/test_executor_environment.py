import os

from app.services.test_executor import TestExecutor


def test_jmeter_launcher_environment_does_not_inherit_reserved_bin_setting(monkeypatch):
    monkeypatch.setenv("JMETER_BIN", r"C:\apache-jmeter-5.6.3\bin\jmeter.bat")

    env = TestExecutor._jmeter_environment()

    assert "JMETER_BIN" not in env
    assert os.environ["JMETER_BIN"] == r"C:\apache-jmeter-5.6.3\bin\jmeter.bat"
