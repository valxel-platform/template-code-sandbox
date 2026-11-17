from __future__ import annotations

from xml.etree import ElementTree as ET

import httpx
import pytest

from deployed import smoke

GOOD = {
    "code runs": "42",
    "no network": "blocked\nno dns",
    "no service account token or secrets": "False\n[]",
    "each run starts clean": "False",
}


def outputs(fresh: bool = True):
    marks: list[bool] = []

    def fake(url, token, code):
        for name, (probe, _) in smoke.PROBES.items():
            if probe == code:
                if name == "each run starts clean":
                    seen = bool(marks) and not fresh
                    marks.append(True)
                    return {"exit_code": 0, "stdout": f"{seen}\n"}
                return {"exit_code": 0, "stdout": GOOD[name] + "\n"}
        raise AssertionError(code)

    return fake


def run(tmp_path, monkeypatch, fake, token="t"):  # noqa: S107 - a placeholder
    monkeypatch.setenv("SMOKE_TOKEN", token)
    monkeypatch.setattr(smoke, "run_code", fake)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(401))
    out = tmp_path / "smoke.xml"
    code = smoke.main(["--base-url", "https://sandbox.test", "--junit", str(out)])
    return code, ET.fromstring(out.read_text())  # noqa: S314 - our own file


def test_an_isolated_sandbox_passes(tmp_path, monkeypatch):
    code, suite = run(tmp_path, monkeypatch, outputs())
    assert code == 0 and suite.get("tests") == "8" and suite.get("failures") == "0"


def test_a_reused_sandbox_or_an_open_network_fails(tmp_path, monkeypatch):
    code, suite = run(tmp_path, monkeypatch, outputs(fresh=False))
    assert code == 1 and suite.get("failures") == "1"
    leaky = outputs()

    def open_net(url, token, code):
        out = leaky(url, token, code)
        return {**out, "stdout": "open\ndns\n"} if "socket" in code else out

    code, suite = run(tmp_path, monkeypatch, open_net)
    assert code == 1


@pytest.mark.parametrize("token", [""])
def test_without_a_token_only_the_outside_checks_run(tmp_path, monkeypatch, token):
    code, suite = run(tmp_path, monkeypatch, outputs(), token=token)
    assert code == 0 and suite.get("tests") == "3"
