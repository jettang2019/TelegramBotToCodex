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
            self.assertEqual(config.bots[0].codex_execution_mode, "full-auto")
            self.assertIsNone(config.bots[0].model)
            self.assertIsNone(config.bots[0].effort)

    def test_load_config_accepts_model_and_effort(self) -> None:
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
                    model = "gpt-5.4"
                    effort = "xhigh"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.bots[0].model, "gpt-5.4")
            self.assertEqual(config.bots[0].effort, "xhigh")

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

    def test_invalid_execution_mode_is_rejected(self) -> None:
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
                    codex_execution_mode = "invalid-mode"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_invalid_effort_is_rejected(self) -> None:
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
                    effort = "extra-high"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)
