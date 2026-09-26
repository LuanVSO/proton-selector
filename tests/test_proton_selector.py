from __future__ import annotations

import csv
import json
import os
import stat
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import proton_selector
import proton_selector_i18n


TOOL_MANIFEST = '''"manifest"
{
  "version" "2"
  "commandline" "/proton %verb%"
  "require_tool_appid" "4183110"
  "use_sessions" "1"
  "compatmanager_layer_name" "proton"
}
'''


def write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def create_custom_tool(parent: Path, directory: str, display_name: str) -> Path:
    tool = parent / directory
    tool.mkdir(parents=True)
    (tool / "compatibilitytool.vdf").write_text(
        f'''"compatibilitytools"
{{
  "compat_tools"
  {{
    "{display_name}"
    {{
      "install_path" "."
      "display_name" "{display_name}"
      "from_oslist" "windows"
      "to_oslist" "linux"
    }}
  }}
}}
''',
        encoding="utf-8",
    )
    (tool / "toolmanifest.vdf").write_text(TOOL_MANIFEST, encoding="utf-8")
    (tool / "version").write_text(f"{display_name}\n", encoding="utf-8")
    write_executable(tool / "proton")
    return tool


class ProtonSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.steam = self.root / "Steam"
        self.compat = self.steam / "compatibilitytools.d"
        self.common = self.steam / "steamapps/common"
        self.compat.mkdir(parents=True)
        self.common.mkdir(parents=True)

        self.base = self.common / "Proton 11.0"
        self.base.mkdir()
        write_executable(self.base / "proton")
        (self.base / "version").write_text("Proton 11.0\n", encoding="utf-8")
        (self.base / "toolmanifest.vdf").write_text(
            TOOL_MANIFEST,
            encoding="utf-8",
        )
        (self.steam / "steamapps/appmanifest_4628710.acf").write_text(
            '''"AppState"
{
  "appid" "4628710"
  "name" "Proton 11.0"
  "installdir" "Proton 11.0"
}
''',
            encoding="utf-8",
        )
        (self.steam / "steamapps/libraryfolders.vdf").write_text(
            f'''"libraryfolders"
{{
  "0"
  {{
    "path" "{self.steam}"
  }}
}}
''',
            encoding="utf-8",
        )

        self.ge_one = create_custom_tool(self.compat, "GE-One", "GE-Proton One")
        self.ge_two = create_custom_tool(self.compat, "GE-Two", "GE-Proton Two")
        self.env = {
            proton_selector.ENV_STEAM_ROOTS: str(self.steam),
            proton_selector.ENV_COMPAT_DIR: str(self.compat),
            proton_selector.ENV_DATA_HOME: str(self.root / "selector-data"),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scans_custom_and_official_tools(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        names = [tool.display_name for tool in tools]
        self.assertEqual(
            names,
            ["GE-Proton One", "GE-Proton Two", "Proton 11.0"],
        )
        base = next(tool for tool in tools if tool.display_name == "Proton 11.0")
        self.assertTrue(base.official)
        self.assertEqual(base.runtime_appid, "4183110")

    def test_scans_installed_steam_games_without_proton_tools(self) -> None:
        (self.steam / "steamapps/appmanifest_12345.acf").write_text(
            '''"AppState"
{
  "appid" "12345"
  "name" "Sample Game"
  "installdir" "Sample Game"
}
''',
            encoding="utf-8",
        )

        games = proton_selector.scan_installed_games(self.home, self.env)

        self.assertEqual(
            [(game.game_id, game.name) for game in games],
            [("12345", "Sample Game")],
        )

    def test_activation_creates_permanent_directory_and_updates_copies(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(
            self.home,
            self.env,
            machine="x86_64",
        )
        base = manager.find_base(tools)
        self.assertIsNotNone(base)
        first = next(tool for tool in tools if tool.display_name == "GE-Proton One")
        second = next(tool for tool in tools if tool.display_name == "GE-Proton Two")

        result = manager.activate(first, base, base)
        self.assertTrue(result.first_activation)
        self.assertTrue(manager.tool_path.is_dir())
        self.assertFalse(manager.tool_path.is_symlink())
        self.assertEqual(manager.current_target(), self.ge_one.resolve())
        self.assertTrue((manager.selected_dir / "proton").is_file())
        self.assertTrue((manager.fallback_dir / "proton").is_file())
        self.assertIn(
            '"Proton Selector"',
            (manager.tool_path / "compatibilitytool.vdf").read_text(),
        )
        self.assertEqual(
            (manager.tool_path / "toolmanifest.vdf").read_text(),
            TOOL_MANIFEST,
        )
        stable_manifest = (
            manager.tool_path / "compatibilitytool.vdf"
        ).read_text()
        launcher_mode = (manager.tool_path / "proton").stat().st_mode
        self.assertTrue(launcher_mode & stat.S_IXUSR)
        selected_metadata = json.loads(
            (manager.selected_dir / proton_selector.COPY_METADATA).read_text()
        )
        fallback_metadata = json.loads(
            (manager.fallback_dir / proton_selector.COPY_METADATA).read_text()
        )
        self.assertEqual(selected_metadata["source_path"], str(self.ge_one.resolve()))
        self.assertEqual(fallback_metadata["source_path"], str(self.base.resolve()))

        result = manager.activate(second, first, base)
        self.assertFalse(result.first_activation)
        self.assertTrue(manager.tool_path.is_dir())
        self.assertFalse(manager.tool_path.is_symlink())
        self.assertEqual(manager.current_target(), self.ge_two.resolve())
        self.assertEqual(
            (manager.tool_path / "compatibilitytool.vdf").read_text(),
            stable_manifest,
        )
        selected_metadata = json.loads(
            (manager.selected_dir / proton_selector.COPY_METADATA).read_text()
        )
        fallback_metadata = json.loads(
            (manager.fallback_dir / proton_selector.COPY_METADATA).read_text()
        )
        self.assertEqual(selected_metadata["source_path"], str(self.ge_two.resolve()))
        self.assertEqual(fallback_metadata["source_path"], str(self.ge_one.resolve()))
        self.assertEqual(manager.current_fallback_target(), self.ge_one.resolve())

    def test_launcher_uses_fallback_when_selected_tool_is_missing(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        selected = next(tool for tool in tools if tool.display_name == "GE-Proton One")
        write_executable(self.base / "proton", "#!/bin/sh\nprintf 'fallback\\n'\n")
        manager.activate(selected, base, base)
        (manager.selected_dir / "proton").unlink()

        completed = subprocess.run(
            [manager.tool_path / "proton", "run"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout, "fallback\n")
        self.assertIn("using fallback", completed.stderr)

    def test_launcher_exports_selected_proton_environment_options(self) -> None:
        write_executable(
            self.ge_one / "proton",
            "#!/bin/sh\nprintf '%s|%s|%s\\n' "
            '"${PROTON_NO_ESYNC-}" "${PROTON_LOG-}" "${PROTON_NO_FSYNC-}"\n',
        )
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        selected = next(tool for tool in tools if tool.display_name == "GE-Proton One")

        manager.activate(
            selected,
            base,
            base,
            proton_environment=("PROTON_NO_ESYNC", "PROTON_LOG", "UNKNOWN_OPTION"),
        )
        completed = subprocess.run(
            [manager.tool_path / "proton", "run"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.stdout, "1|1|\n")
        self.assertEqual(
            manager.proton_environment_options(),
            {"PROTON_NO_ESYNC", "PROTON_LOG"},
        )

    def test_matching_version_file_skips_existing_managed_copy(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        selected = next(tool for tool in tools if tool.display_name == "GE-Proton One")
        manager.activate(selected, base, base)
        sentinel = manager.selected_dir / "keep-existing-copy"
        sentinel.write_text("preserved\n", encoding="utf-8")

        write_executable(self.ge_one / "proton", "#!/bin/sh\nprintf 'changed\\n'\n")
        manager.activate(selected, base, base)

        self.assertTrue(sentinel.is_file())
        self.assertNotIn(
            "changed",
            (manager.selected_dir / "proton").read_text(encoding="utf-8"),
        )

    def test_game_ids_select_and_update_managed_game_copies(self) -> None:
        write_executable(self.ge_one / "proton", "#!/bin/sh\nprintf 'active\\n'\n")
        write_executable(self.ge_two / "proton", "#!/bin/sh\nprintf 'game-two\\n'\n")
        write_executable(self.base / "proton", "#!/bin/sh\nprintf 'base\\n'\n")
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        first = next(tool for tool in tools if tool.display_name == "GE-Proton One")
        second = next(tool for tool in tools if tool.display_name == "GE-Proton Two")

        manager.activate(
            first,
            base,
            base,
            game_id="12345",
            game_tool=second,
        )
        manager.activate(
            first,
            base,
            base,
            game_id="umu-example",
            game_tool=base,
        )

        with manager.game_mappings_path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as mappings_file:
            rows = {row["game_id"]: row for row in csv.DictReader(mappings_file)}
        self.assertEqual(set(rows), {"12345", "umu-example"})
        self.assertEqual(rows["12345"]["proton_name"], "GE-Proton Two")
        game_copy = (
            manager.games_dir
            / rows["12345"]["managed_directory"]
            / "proton"
        )
        self.assertTrue(game_copy.is_file())

        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "SteamGameId",
                "GAMEID",
                "UMU_ID",
                "STEAM_COMPAT_APP_ID",
                "SteamAppId",
            }
        }
        steam_game = subprocess.run(
            [manager.tool_path / "proton", "run"],
            check=True,
            capture_output=True,
            text=True,
            env={**clean_env, "SteamGameId": "12345"},
        )
        umu_game = subprocess.run(
            [manager.tool_path / "proton", "run"],
            check=True,
            capture_output=True,
            text=True,
            env={**clean_env, "GAMEID": "umu-example"},
        )
        unmatched_game = subprocess.run(
            [manager.tool_path / "proton", "run"],
            check=True,
            capture_output=True,
            text=True,
            env={**clean_env, "SteamGameId": "99999"},
        )
        self.assertEqual(steam_game.stdout, "game-two\n")
        self.assertEqual(umu_game.stdout, "base\n")
        self.assertEqual(unmatched_game.stdout, "active\n")

        manager.activate(
            first,
            base,
            base,
            game_id="12345",
            game_tool=first,
        )
        mappings = manager.game_mappings()
        self.assertEqual(len(mappings), 2)
        self.assertEqual(mappings["12345"].source_path, str(self.ge_one.resolve()))
        self.assertTrue(manager.game_copy_available(mappings["12345"]))
        changed_game = subprocess.run(
            [manager.tool_path / "proton", "run"],
            check=True,
            capture_output=True,
            text=True,
            env={**clean_env, "SteamGameId": "12345"},
        )
        self.assertEqual(changed_game.stdout, "active\n")
        managed_directory = mappings["12345"].managed_directory
        self.assertTrue(manager.clear_game_mapping("12345"))
        self.assertNotIn("12345", manager.game_mappings())
        self.assertIn("umu-example", manager.game_mappings())
        self.assertFalse((manager.games_dir / managed_directory).exists())
        self.assertFalse(manager.clear_game_mapping("12345"))

    def test_rejects_unsafe_game_id(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        selected = next(tool for tool in tools if tool.display_name == "GE-Proton One")

        with self.assertRaisesRegex(RuntimeError, "GAME ID"):
            manager.activate(
                selected,
                base,
                base,
                game_id="../bad,id",
                game_tool=selected,
            )

    def test_cli_updates_versions_and_game_mapping(self) -> None:
        command = [sys.executable, str(Path(proton_selector.__file__).resolve())]
        environment = {
            **os.environ,
            **self.env,
            "HOME": str(self.home),
            "LANG": "C",
            "LC_ALL": "C",
            "PROTON_SELECTOR_LANGUAGE": "en",
        }

        listed = subprocess.run(
            [*command, "--list-versions"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertIn("GE-Proton One", listed.stdout)
        self.assertIn(str(self.ge_one.resolve()), listed.stdout)

        changed = subprocess.run(
            [
                *command,
                "--active",
                "GE-Proton One",
                "--fallback",
                "Proton 11.0",
                "--game-id",
                "230410",
                "--game",
                "GE-Proton Two",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertIn("Game 230410 now uses GE-Proton Two.", changed.stdout)

        manager = proton_selector.SelectorManager(self.home, self.env)
        self.assertEqual(manager.current_target(), self.ge_one.resolve())
        self.assertEqual(
            manager.game_mapping("230410").source_path,
            str(self.ge_two.resolve()),
        )

        status = subprocess.run(
            [*command, "--status"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertIn("Active version: GE-Proton One", status.stdout)
        self.assertIn("Fallback version: Proton 11.0", status.stdout)
        self.assertIn("Game 230410: GE-Proton Two", status.stdout)

    def test_existing_unrelated_selector_link_is_not_replaced(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        selected = next(tool for tool in tools if tool.display_name == "GE-Proton One")
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        manager.tool_path.symlink_to(unrelated, target_is_directory=True)

        with self.assertRaisesRegex(RuntimeError, "unrelated symlink"):
            manager.activate(selected, base, base)
        self.assertEqual(manager.tool_path.resolve(), unrelated.resolve())

    def test_migrates_legacy_selector_symlink_to_real_directory(self) -> None:
        tools = proton_selector.scan_proton_tools(self.home, self.env)
        manager = proton_selector.SelectorManager(self.home, self.env)
        base = manager.find_base(tools)
        selected = next(tool for tool in tools if tool.display_name == "GE-Proton One")
        manager.legacy_adapter_path.mkdir(parents=True)
        manager.tool_path.symlink_to(
            manager.legacy_adapter_path,
            target_is_directory=True,
        )

        result = manager.activate(selected, base, base)

        self.assertFalse(result.first_activation)
        self.assertTrue(manager.tool_path.is_dir())
        self.assertFalse(manager.tool_path.is_symlink())
        self.assertTrue((manager.selected_dir / "proton").is_file())
        self.assertTrue((manager.fallback_dir / "proton").is_file())

    def test_arm_uses_arm64_base_name(self) -> None:
        self.assertEqual(
            proton_selector.required_base_name("aarch64"),
            "Proton 11.0 (ARM64)",
        )


class LocalizationTests(unittest.TestCase):
    def test_every_catalog_is_complete_and_preserves_placeholders(self) -> None:
        english = proton_selector_i18n.CATALOGS["en"]
        expected_keys = set(proton_selector_i18n.MESSAGE_IDS)
        formatter = string.Formatter()

        def fields(message: str) -> set[str]:
            return {
                field_name
                for _literal, field_name, _format_spec, _conversion
                in formatter.parse(message)
                if field_name
            }

        for language, catalog in proton_selector_i18n.CATALOGS.items():
            with self.subTest(language=language):
                self.assertEqual(set(catalog), expected_keys)
                for message_id, message in catalog.items():
                    self.assertEqual(fields(message), fields(english[message_id]))

    def test_system_language_selection_and_regional_variants(self) -> None:
        cases = (
            ({"LANG": "es_MX.UTF-8"}, "es"),
            ({"LC_ALL": "de_DE.UTF-8", "LANG": "es_ES.UTF-8"}, "de"),
            ({"LANGUAGE": "unsupported:uk:en"}, "uk"),
            ({"LANG": "pt_PT.UTF-8"}, "pt_BR"),
            ({"LANG": "zh_HK.UTF-8"}, "zh_TW"),
            ({"LANG": "zh_SG.UTF-8"}, "zh_CN"),
            ({"LANG": "C"}, "en"),
            (
                {
                    "PROTON_SELECTOR_LANGUAGE": "ja",
                    "LANG": "fr_FR.UTF-8",
                },
                "ja",
            ),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment):
                self.assertEqual(
                    proton_selector_i18n.select_language(environment),
                    expected,
                )

    def test_translator_formats_localized_dynamic_text(self) -> None:
        translator = proton_selector_i18n.Translator(language="es")
        self.assertEqual(
            translator("game_success", game_id="230410", name="GE-Proton10-34"),
            "El juego 230410 ahora usa GE-Proton10-34.",
        )

    def test_language_preference_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            environment = {"XDG_CONFIG_HOME": str(root / "config")}
            proton_selector.save_language_preference("ja", home, environment)
            self.assertEqual(
                proton_selector.saved_language_preference(home, environment),
                "ja",
            )
            proton_selector.save_language_preference(
                "system",
                home,
                environment,
            )
            self.assertEqual(
                proton_selector.saved_language_preference(home, environment),
                "system",
            )


if __name__ == "__main__":
    unittest.main()
