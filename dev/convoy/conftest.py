import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import convoy_artifacts


@pytest.fixture(autouse=True)
def isolate_default_artifact_cache(tmp_path, monkeypatch):
    """Never let a HostApp test touch the production per-user cache.

    HostApp deliberately defaults outside its state directory in production.
    Most pre-artifact tests construct HostApp directly, so a shared fixture is
    the only reliable cross-platform fence (including macOS CI).  Explicit
    platform arguments still exercise the real path function unchanged.
    """
    original = convoy_artifacts.default_cache_root
    isolated = str(tmp_path / "default-artifact-cache")

    def test_default(platform=None, environ=None, home=None):
        if platform is None and environ is None and home is None:
            return isolated
        return original(platform=platform, environ=environ, home=home)

    monkeypatch.setattr(convoy_artifacts, "default_cache_root", test_default)


def approve_td_python(app, node_id):
    """Exercise the real one-shot local policy grant in host-app tests."""
    if app.policy.allow_td_python(node_id):
        return app.policy.snapshot()
    generation = app.policy.generation
    challenge = app.policy.begin_enable_td_python(
        node_id, expected_generation=generation)
    return app.policy.confirm_enable(
        challenge["challenge_id"], challenge["confirmation"],
        expected_generation=generation)
