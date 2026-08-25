from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_application_dockerfile_installs_bubblewrap() -> None:
    dockerfile = (ROOT / "apps/Dockerfile").read_text()
    install = dockerfile[dockerfile.index("RUN apt-get update") :]

    assert "bubblewrap" in install


def test_app_image_check_invokes_real_bwrap_as_runtime_uid() -> None:
    script = (ROOT / "scripts/check-app-sandbox-image.sh").read_text()

    assert "--user 10700:10700" in script
    assert "--cap-drop ALL" in script
    assert "no-new-privileges:true" in script
    assert "SandboxExecutor" in script
