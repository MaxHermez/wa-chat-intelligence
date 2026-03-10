"""Tests for wain.config -- config resolution chain."""

from pathlib import Path

import pytest

from wain import config


class TestResolveChain:
    def test_cli_overrides_highest_priority(self, clean_config):
        config.set_cli_overrides(sender_self="FromCLI")
        val, src = config._resolve_with_source(
            "sender_self", "FromEnv"
        )
        assert val == "FromCLI"
        assert src == "cli"

    def test_env_fallback(self, clean_config):
        val, src = config._resolve_with_source(
            "sender_self", "FromEnv"
        )
        assert val == "FromEnv"

    def test_clear_overrides(self, clean_config):
        config.set_cli_overrides(sender_self="FromCLI")
        config.clear_cli_overrides()
        val, _ = config._resolve_with_source(
            "sender_self", "Default"
        )
        assert val == "Default"

    def test_none_values_ignored(self, clean_config):
        config.set_cli_overrides(sender_self=None, sender_other="Bob")
        assert "sender_self" not in config._cli_overrides
        assert config._cli_overrides["sender_other"] == "Bob"


class TestWorkspacePaths:
    def test_get_workspace_paths(self):
        ws = config.get_workspace_paths("test-ws")
        assert ws.name == "test-ws"
        assert "test-ws" in ws.root
        assert ws.db_path.endswith("chat.db")
        assert ws.index_path.endswith("chat.faiss")

    def test_active_paths_default(self):
        # With no workspace set, active paths return module defaults
        assert config.active_db_path() == config.DB_PATH
        assert config.active_index_path() == config.INDEX_PATH


class TestTomlLoading:
    def test_load_missing_file(self):
        result = config._load_toml(Path("/nonexistent/path/config.toml"))
        assert result == {}

    def test_load_valid_toml(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[defaults]\nsender_self = "Alice"\n',
            encoding="utf-8",
        )
        result = config._load_toml(toml_file)
        assert result["defaults"]["sender_self"] == "Alice"


class TestWriteToml:
    def test_write_and_read_back(self, tmp_path):
        path = tmp_path / "sub" / "config.toml"
        data = {"defaults": {"sender_self": "Alice"}}
        config.write_toml(path, data)
        assert path.exists()
        loaded = config._load_toml(path)
        assert loaded["defaults"]["sender_self"] == "Alice"
