import tempfile
import textwrap
import unittest
from pathlib import Path

from telegram_bot_to_codex.config import ConfigError, load_config, normalize_username


class ConfigTests(unittest.TestCase):
    def test_normalize_username_accepts_leading_at(self) -> None:
        self.assertEqual(normalize_username("@User_Name"), "user_name")

    def test_load_config_resolves_relative_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workdir = root / "repo"
            workdir.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    [app]
                    state_path = ".local/state.json"

                    [[bots]]
                    name = "demo"
                    token = "123:abc"
                    workdir = "{workdir}"
                    telegram_username = "@demo"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.app.state_path, (root / ".local/state.json").resolve())
            self.assertEqual(config.bots[0].normalized_username, "demo")

    def test_duplicate_bot_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workdir = root / "repo"
            workdir.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    [app]

                    [[bots]]
                    name = "demo"
                    token = "123:abc"
                    workdir = "{workdir}"
                    telegram_username = "@demo"

                    [[bots]]
                    name = "demo"
                    token = "123:def"
                    workdir = "{workdir}"
                    telegram_username = "@demo2"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)
