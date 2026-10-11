import os
import subprocess
import sys

from rnsfips.cli import main, render_units


def test_render_units_paths():
    units = render_units("/opt/v/bin/rnsd", "/opt/v/bin/rnstatus", "/opt/v/bin/rnsfips", "/etc/rnsfips/tunnel.conf")
    assert "ExecStart=/opt/v/bin/rnsd -v" in units["rnsd.service"]
    t = units["rnsfips-tunnel.service"]
    assert "ExecStart=/opt/v/bin/rnsfips tunnel --config /etc/rnsfips/tunnel.conf -v" in t
    assert "/opt/v/bin/rnstatus" in t and "Requires=rnsd.service" in t


def test_install_units_writes_files(tmp_path, capsys):
    rc = main(["install-units", "--unit-dir", str(tmp_path)])
    assert rc == 0
    assert sorted(os.listdir(tmp_path)) == ["rnsd.service", "rnsfips-tunnel.service"]
    body = (tmp_path / "rnsd.service").read_text()
    assert "ExecStart=" in body and "WantedBy=default.target" in body
    if subprocess.run(["which", "systemd-analyze"], capture_output=True).returncode == 0:
        r = subprocess.run(["systemd-analyze", "--user", "verify", str(tmp_path / "rnsd.service"),
                            str(tmp_path / "rnsfips-tunnel.service")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
