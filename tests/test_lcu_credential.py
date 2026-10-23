"""LCU 凭据发现与解析测试。"""

from __future__ import annotations

from src.lcu.credential import (
    LcuCredentialProvider,
    LcuCredentials,
    parse_command_line,
)

SAMPLE_COMMAND_LINE = (
    'd:/lol(26)/LeagueClient/LeagueClientUx.exe '
    '"--riotclient-auth-token=TEST-TOKEN-NOT-REAL-01" '
    '"--riotclient-app-port=1303" "--riotclient-tencent" "--no-rads" '
    '"--region=TENCENT" "--locale=zh_CN" '
    '"--rso_platform_id=TJ100" '
    '"--remoting-auth-token=TEST-TOKEN-NOT-REAL-02" "--app-port=3485" '
    '"--app-pid=51216"'
)


def test_parse_command_line_extracts_credentials() -> None:
    creds = parse_command_line(SAMPLE_COMMAND_LINE)
    assert creds is not None
    assert creds.port == 3485
    assert creds.token == "TEST-TOKEN-NOT-REAL-02"
    assert creds.protocol == "https"
    assert creds.base_url == "https://127.0.0.1:3485"


def test_parse_command_line_keeps_region_and_locale() -> None:
    creds = parse_command_line(SAMPLE_COMMAND_LINE)
    assert creds is not None
    assert creds.region == "TENCENT"
    assert creds.locale == "zh_CN"
    assert creds.rso_platform_id == "TJ100"
    assert creds.app_pid == 51216


def test_region_and_locale_are_not_required_for_credentials() -> None:
    minimal = "LeagueClientUx.exe --app-port=5555 --remoting-auth-token=zzz"
    creds = parse_command_line(minimal)
    assert creds is not None
    assert creds.port == 5555
    assert creds.region == ""
    assert creds.locale == ""


def test_parse_command_line_returns_none_without_token_or_port() -> None:
    assert parse_command_line("") is None
    assert parse_command_line("LeagueClientUx.exe --region=TENCENT") is None
    assert parse_command_line("LeagueClientUx.exe --app-port=1234") is None


def test_safe_repr_never_leaks_token() -> None:
    creds = parse_command_line(SAMPLE_COMMAND_LINE)
    assert creds is not None
    text = creds.safe_repr()
    assert "TEST-TOKEN-NOT-REAL-02" not in text
    assert "token_length=%d" % len("TEST-TOKEN-NOT-REAL-02") in text


def test_find_credentials_does_not_raise_on_empty_lockfile() -> None:
    """国服 lockfile 是 0 字节，provider 不应因此报错。"""
    provider = LcuCredentialProvider()
    result = provider.find_credentials()
    assert result is None or isinstance(result, LcuCredentials)
