"""Isolated deployment-script check: command ordering and invalid config rollback."""

import os
import shutil
import subprocess
from pathlib import Path


def test_deploy_success_and_config_rollback(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / "docker").mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("deploy.sh"), root / "deploy.sh")
    (root / ".env").write_text("# isolated test\n")
    (root / "docker-compose.yml").write_text("services: {}\n")
    candidate = root / "docker" / "nginx-stock.conf"
    candidate.write_text("new config\n")

    nginx_dir = tmp_path / "nginx"
    site = nginx_dir / "sites-enabled" / "stock.conf"
    site.parent.mkdir(parents=True)
    site.write_text("old config\n")
    log = tmp_path / "commands.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'echo "docker $*" >> "$COMMAND_LOG"\n'
        'if [ "$1 $2 $3 ${4:-}" = "exec nginx nginx -t" ] && '
        'grep -q INVALID "$NGINX_DIR/sites-enabled/stock.conf"; then exit 1; fi\n'
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\necho "curl $*" >> "$COMMAND_LOG"\n')
    curl.chmod(0o755)
    make = bin_dir / "make"
    make.write_text('#!/bin/sh\necho "make $*" >> "$COMMAND_LOG"\n')
    make.chmod(0o755)
    env = {
        **os.environ,
        "NGINX_DIR": str(nginx_dir),
        "COMMAND_LOG": str(log),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(root / "deploy.sh")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    result = run()
    assert result.returncode == 0, result.stderr
    assert site.read_text() == "new config\n"
    assert len(list(site.parent.glob("stock.conf.bak.*"))) == 1
    calls = log.read_text().splitlines()
    build = next(i for i, line in enumerate(calls) if " build web" in line)
    up = next(i for i, line in enumerate(calls) if " up -d " in line)
    reload = next(i for i, line in enumerate(calls) if "nginx -s reload" in line)
    check = calls.index("make check")
    assert check < build < up < reload
    assert any(line.startswith("curl ") for line in calls[up:reload])
    assert any(line.startswith("curl ") for line in calls[reload:])

    log.write_text("")
    candidate.write_text("INVALID config\n")
    result = run()
    assert result.returncode != 0
    assert site.read_text() == "new config\n"
    assert "up -d" not in log.read_text()
    assert "nginx -s reload" not in log.read_text()
