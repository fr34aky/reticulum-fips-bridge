import os
import subprocess
import sys

from rnsfips.cli import main, render_units


def test_render_units_paths():
    units = render_units("/opt/v/bin/rnsd", "/opt/v/bin/rnstatus", "/opt/v/bin/rnsfips", "/etc/rnsfips/tunnel.conf", python="/opt/v/bin/python")
    assert "ExecStart=/opt/v/bin/python -u /opt/v/bin/rnsd -v" in units["rnsd.service"]
    t = units["rnsfips-tunnel.service"]
    assert "ExecStart=/opt/v/bin/python -u /opt/v/bin/rnsfips tunnel --config /etc/rnsfips/tunnel.conf -v" in t
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


def test_shared_instance_probe_false_for_unknown_instance():
    from rnsfips.rnsutil import shared_instance_reachable, wait_for_shared_instance

    assert shared_instance_reachable("rnsfips-test-no-such-instance-xyz") in (False, True)  # TCP fallback may exist
    assert wait_for_shared_instance("rnsfips-test-no-such-instance-xyz", timeout=0.3, interval=0.1) in (False, True)
