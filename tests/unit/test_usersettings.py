"""User config file and Mistral key loading (ADDENDUM F)."""

from __future__ import annotations

from pathlib import Path

import pytest

from morning_star_review.core.errors import MsreviewSecretCommandError
from morning_star_review.usersettings import (
    SettingsError,
    UserSettings,
    cache_dir,
    config_path,
    default_agent_python,
    load_mistral_key,
    load_user_settings,
)


def write_config(tmp_path: Path, text: str) -> dict[str, str]:
    path = tmp_path / ".config" / "morning-star-review" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_user_settings({"HOME": str(tmp_path)}) == UserSettings()


def test_reads_mistral_and_defaults(tmp_path: Path) -> None:
    env = write_config(
        tmp_path,
        '[mistral]\napi_key_command = "printf k"\n'
        '[defaults]\npreset = "balanced"\nmax_cost_usd = 2\nread_token = "gh"\n'
        'allowed_models = ["zai-glm-5-3"]\nfail_on = "high"\n'
        '[agent]\npython = "/opt/agent/bin/python"\n'
        '[app]\nclient_id = "ignored here"\n',
    )
    s = load_user_settings(env)
    assert s.mistral_api_key_command == "printf k"
    assert (s.preset, s.max_cost_usd, s.read_token, s.fail_on) == ("balanced", 2.0, "gh", "high")
    assert s.allowed_models == ["zai-glm-5-3"]
    assert s.agent_python == "/opt/agent/bin/python"
    assert "printf" not in repr(s)


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "mistral = 3",
        '[defaults]\nmax_cost_usd = "ten"',
        '[defaults]\nread_token = "maybe"',
        "[defaults]\nallowed_models = [1]",
        "[mistral]\napi_key_command = 5",
    ],
)
def test_invalid_files(tmp_path: Path, text: str) -> None:
    env = write_config(tmp_path, text)
    with pytest.raises(SettingsError):
        load_user_settings(env)


def test_key_order_env_command_first(tmp_path: Path) -> None:
    settings = UserSettings(mistral_api_key_command="printf from-file")
    env = {"PATH": "/usr/bin:/bin", "MISTRAL_API_KEY": "from-env"}
    assert (
        load_mistral_key({**env, "MSREVIEW_MISTRAL_API_KEY_COMMAND": "printf from-cmd"}, settings)
        == "from-cmd"
    )
    assert load_mistral_key(env, settings) == "from-env"
    assert load_mistral_key({"PATH": "/usr/bin:/bin"}, settings) == "from-file"
    with pytest.raises(SettingsError):
        load_mistral_key({"PATH": "/usr/bin:/bin"}, UserSettings())


def test_failing_key_command_does_not_leak(tmp_path: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "MSREVIEW_MISTRAL_API_KEY_COMMAND": "echo sekret-value-1234; exit 3",
    }
    with pytest.raises(MsreviewSecretCommandError) as exc:
        load_mistral_key(env, UserSettings())
    assert "sekret-value-1234" not in str(exc.value)


def test_paths(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    assert config_path(env) == tmp_path / ".config" / "morning-star-review" / "config.toml"
    assert config_path({**env, "MSREVIEW_CONFIG": "/x/c.toml"}) == Path("/x/c.toml")
    assert cache_dir(env) == tmp_path / ".cache" / "morning-star-review"
    assert cache_dir({**env, "XDG_CACHE_HOME": "/xdg"}) == Path("/xdg/morning-star-review")
    assert default_agent_python(env).parts[-3:] == (
        default_agent_python(env).parts[-3],
        "bin",
        "python",
    )
    assert default_agent_python(env).parts[-3].startswith("agent-")
