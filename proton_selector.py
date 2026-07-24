#!/usr/bin/env python3
"""Select the Proton build used by Steam's stable "Proton Selector" tool."""

from __future__ import annotations

import argparse
import ctypes
import csv
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from proton_selector_i18n import (
    CATALOGS,
    LANGUAGE_NAMES,
    select_language,
    translate as _,
)


APP_NAME = "Proton Selector"
SLOT_NAME = "Proton Selector"
ENV_STEAM_ROOTS = "PROTON_SELECTOR_STEAM_ROOTS"
ENV_COMPAT_DIR = "PROTON_SELECTOR_COMPAT_DIR"
ENV_DATA_HOME = "PROTON_SELECTOR_DATA_HOME"
COPY_METADATA = ".proton-selector-source.json"
GAME_MAPPINGS_CSV = "game-mappings.csv"
LANGUAGE_PREFERENCE = "language"
GAME_MAPPING_FIELDS = (
    "game_id",
    "managed_directory",
    "proton_name",
    "source_path",
)
GAME_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

COMPATIBILITY_TOOL_VDF = '''"compatibilitytools"
{
  "compat_tools"
  {
    "Proton Selector"
    {
      "install_path" "."
      "display_name" "Proton Selector"
      "from_oslist" "windows"
      "to_oslist" "linux"
    }
  }
}
'''

PROTON_LAUNCHER = r'''#!/bin/sh
set -eu

selector_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
active="$selector_dir/selected"
fallback="$selector_dir/fallback"
selected="$active"

lookup_game_copy()
{
    game_id=$1
    [ -n "$game_id" ] || return 1
    [ -f "$selector_dir/game-mappings.csv" ] || return 1
    managed_directory=$(
        awk -F, -v game_id="$game_id" \
            'NR > 1 && $1 == game_id { print $2; exit }' \
            "$selector_dir/game-mappings.csv"
    )
    [ -n "$managed_directory" ] || return 1
    candidate="$selector_dir/games/$managed_directory"
    [ -x "$candidate/proton" ] || return 1
    selected="$candidate"
    PROTON_SELECTOR_GAME_ID="$game_id"
    export PROTON_SELECTOR_GAME_ID
    return 0
}

for candidate_id in \
    "${SteamGameId-}" \
    "${GAMEID-}" \
    "${UMU_ID-}" \
    "${STEAM_COMPAT_APP_ID-}" \
    "${SteamAppId-}"
do
    if lookup_game_copy "$candidate_id"; then
        break
    fi
done

if [ -z "$selected" ] || [ ! -x "$selected/proton" ]; then
    if [ "$selected" != "$active" ] && [ -x "$active/proton" ]; then
        printf '%s\n' \
            "Proton Selector: game Proton is unavailable; using active." >&2
        selected=$active
    else
        printf '%s\n' \
            "Proton Selector: active Proton is unavailable; using fallback." >&2
        selected=$fallback
    fi
fi

if [ -z "$selected" ] || [ ! -x "$selected/proton" ]; then
    printf '%s\n' "Proton Selector: fallback Proton is also unavailable." >&2
    exit 1
fi

# Steam normally puts the registered compatibility tool first in this list.
# Point that entry at the selected build so Proton and its child processes see
# the actual tool location.
if [ "${STEAM_COMPAT_TOOL_PATHS+x}" = x ]; then
    case "$STEAM_COMPAT_TOOL_PATHS" in
        *:*) STEAM_COMPAT_TOOL_PATHS="$selected:${STEAM_COMPAT_TOOL_PATHS#*:}" ;;
        *)   STEAM_COMPAT_TOOL_PATHS="$selected" ;;
    esac
    export STEAM_COMPAT_TOOL_PATHS
fi

if [ "${STEAM_COMPAT_TOOL_PATH+x}" = x ]; then
    STEAM_COMPAT_TOOL_PATH="$selected"
    export STEAM_COMPAT_TOOL_PATH
fi

PROTON_SELECTOR_TARGET="$selected"
export PROTON_SELECTOR_TARGET
exec "$selected/proton" "$@"
'''


@dataclass(frozen=True)
class ProtonTool:
    display_name: str
    path: Path
    source: str
    official: bool = False
    runtime_appid: str = ""

    @property
    def canonical_path(self) -> Path:
        return self.path.resolve()


@dataclass(frozen=True)
class ActivationResult:
    first_activation: bool
    tool_path: Path
    selected_path: Path


@dataclass(frozen=True)
class GameMapping:
    game_id: str
    managed_directory: str
    proton_name: str
    source_path: str


def normalize_game_id(value: str) -> str:
    game_id = value.strip()
    if not game_id:
        return ""
    if not GAME_ID_PATTERN.fullmatch(game_id):
        raise RuntimeError(_("invalid_game_id"))
    return game_id


def managed_game_directory(game_id: str) -> str:
    digest = hashlib.sha256(game_id.encode("utf-8")).hexdigest()[:16]
    return f"game-{digest}"


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        try:
            key = str(expanded.resolve())
        except OSError:
            key = str(expanded.absolute())
        if key in seen:
            continue
        seen.add(key)
        result.append(expanded)
    return result


def _vdf_value(text: str, key: str) -> str:
    pattern = rf'"{re.escape(key)}"\s*"((?:\\.|[^"\\])*)"'
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return ""
    return (
        match.group(1)
        .replace(r"\\", "\\")
        .replace(r"\"", '"')
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _runtime_appid(tool_path: Path) -> str:
    return _vdf_value(_read_text(tool_path / "toolmanifest.vdf"), "require_tool_appid")


def required_base_name(machine: str | None = None) -> str:
    architecture = (machine or platform.machine()).lower()
    if architecture in {"aarch64", "arm64"}:
        return "Proton 11.0 (ARM64)"
    return "Proton 11.0"


def discover_steam_roots(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    home = home or Path.home()
    env = env or os.environ
    roots: list[Path] = []

    if env.get(ENV_STEAM_ROOTS):
        roots.extend(
            Path(item)
            for item in env[ENV_STEAM_ROOTS].split(os.pathsep)
            if item
        )

    xdg_data = Path(env.get("XDG_DATA_HOME", home / ".local/share"))
    roots.extend(
        [
            home / ".steam/root",
            home / ".steam/steam",
            xdg_data / "Steam",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",
            home / ".var/app/com.valvesoftware.Steam/.steam/root",
            home / "snap/steam/common/.local/share/Steam",
        ]
    )
    return _unique_paths(path for path in roots if path.exists())


def discover_library_roots(steam_roots: Iterable[Path]) -> list[Path]:
    libraries: list[Path] = list(steam_roots)
    for steam_root in steam_roots:
        for library_file in (
            steam_root / "steamapps/libraryfolders.vdf",
            steam_root / "config/libraryfolders.vdf",
        ):
            text = _read_text(library_file)
            if not text:
                continue
            for match in re.finditer(
                r'"path"\s*"((?:\\.|[^"\\])*)"',
                text,
                flags=re.IGNORECASE,
            ):
                value = match.group(1).replace(r"\\", "\\").replace(r"\"", '"')
                if value:
                    libraries.append(Path(value))
    return _unique_paths(path for path in libraries if path.exists())


def discover_compatibility_directories(
    steam_roots: Iterable[Path],
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    env = env or os.environ
    directories = [root / "compatibilitytools.d" for root in steam_roots]
    directories.extend(
        [
            Path("/usr/share/steam/compatibilitytools.d"),
            Path("/usr/local/share/steam/compatibilitytools.d"),
        ]
    )

    extra = env.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "")
    directories.extend(Path(item) for item in extra.split(os.pathsep) if item)
    return _unique_paths(path for path in directories if path.is_dir())


def _custom_tool_from_directory(directory: Path, source: str) -> ProtonTool | None:
    if directory.name == SLOT_NAME:
        return None
    manifest = directory / "compatibilitytool.vdf"
    launcher = directory / "proton"
    if not manifest.is_file() or not launcher.is_file():
        return None
    display_name = _vdf_value(_read_text(manifest), "display_name") or directory.name
    return ProtonTool(
        display_name=display_name,
        path=directory,
        source=source,
        runtime_appid=_runtime_appid(directory),
    )


def scan_custom_tools(compatibility_directories: Iterable[Path]) -> list[ProtonTool]:
    tools: list[ProtonTool] = []
    for compatibility_dir in compatibility_directories:
        source = str(compatibility_dir)
        try:
            children = sorted(
                compatibility_dir.iterdir(),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            continue

        for child in children:
            try:
                if child.is_dir():
                    tool = _custom_tool_from_directory(child, source)
                    if tool:
                        tools.append(tool)
                    continue

                if child.suffix.lower() != ".vdf":
                    continue
                text = _read_text(child)
                if '"compat_tools"' not in text.lower():
                    continue
                install_path = _vdf_value(text, "install_path")
                if not install_path:
                    continue
                tool_path = Path(install_path)
                if not tool_path.is_absolute():
                    tool_path = child.parent / tool_path
                launcher = tool_path / "proton"
                if not launcher.is_file():
                    continue
                display_name = _vdf_value(text, "display_name") or tool_path.name
                tools.append(
                    ProtonTool(
                        display_name=display_name,
                        path=tool_path,
                        source=source,
                        runtime_appid=_runtime_appid(tool_path),
                    )
                )
            except OSError:
                continue
    return tools


def scan_official_tools(library_roots: Iterable[Path]) -> list[ProtonTool]:
    tools: list[ProtonTool] = []
    for library_root in library_roots:
        steamapps = library_root / "steamapps"
        try:
            manifests = steamapps.glob("appmanifest_*.acf")
        except OSError:
            continue

        for manifest in manifests:
            text = _read_text(manifest)
            name = _vdf_value(text, "name")
            install_dir = _vdf_value(text, "installdir")
            if not name.casefold().startswith("proton") or not install_dir:
                continue
            tool_path = steamapps / "common" / install_dir
            if not (tool_path / "proton").is_file():
                continue
            tools.append(
                ProtonTool(
                    display_name=name,
                    path=tool_path,
                    source=_("source_steam_library", path=library_root),
                    official=True,
                    runtime_appid=_runtime_appid(tool_path),
                )
            )
    return tools


def scan_proton_tools(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ProtonTool]:
    home = home or Path.home()
    env = env or os.environ
    steam_roots = discover_steam_roots(home, env)
    libraries = discover_library_roots(steam_roots)
    compatibility_dirs = discover_compatibility_directories(steam_roots, env)

    candidates = scan_custom_tools(compatibility_dirs)
    candidates.extend(scan_official_tools(libraries))

    # The same directory is often reachable through ~/.steam/root,
    # ~/.steam/steam, and ~/.local/share/Steam. Prefer the official manifest's
    # name when Steam itself owns the installation.
    deduplicated: dict[str, ProtonTool] = {}
    for tool in candidates:
        try:
            key = str(tool.canonical_path)
        except OSError:
            continue
        existing = deduplicated.get(key)
        if existing is None or (tool.official and not existing.official):
            deduplicated[key] = tool

    return sorted(
        deduplicated.values(),
        key=lambda tool: (tool.display_name.casefold(), str(tool.path)),
    )


def choose_compatibility_directory(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    home = home or Path.home()
    env = env or os.environ
    if env.get(ENV_COMPAT_DIR):
        return Path(env[ENV_COMPAT_DIR]).expanduser()

    candidates = [
        home / ".steam/root/compatibilitytools.d",
        home / ".steam/steam/compatibilitytools.d",
        Path(env.get("XDG_DATA_HOME", home / ".local/share"))
        / "Steam/compatibilitytools.d",
        home
        / ".var/app/com.valvesoftware.Steam/data/Steam/compatibilitytools.d",
        home
        / ".var/app/com.valvesoftware.Steam/.steam/root/compatibilitytools.d",
        home / "snap/steam/common/.local/share/Steam/compatibilitytools.d",
    ]
    for candidate in candidates:
        if candidate.parent.exists():
            return candidate
    return candidates[0]


def data_home(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    home = home or Path.home()
    env = env or os.environ
    if env.get(ENV_DATA_HOME):
        return Path(env[ENV_DATA_HOME]).expanduser()
    return Path(env.get("XDG_DATA_HOME", home / ".local/share")) / "proton-selector"


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def language_preference_path(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    home = home or Path.home()
    env = env or os.environ
    config_home = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
    return config_home / "proton-selector" / LANGUAGE_PREFERENCE


def saved_language_preference(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    preference = _read_text(language_preference_path(home, env)).strip()
    if preference == "system" or preference in CATALOGS:
        return preference
    return "system"


def save_language_preference(
    preference: str,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    if preference != "system" and preference not in CATALOGS:
        raise ValueError(f"Unsupported language preference: {preference}")
    path = language_preference_path(home, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, f"{preference}\n")


def _remove_managed_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _exchange_paths(first: Path, second: Path) -> bool:
    """Atomically exchange two Linux filesystem entries when supported."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    result = renameat2(
        at_fdcwd,
        os.fsencode(first),
        at_fdcwd,
        os.fsencode(second),
        rename_exchange,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {38, 95, 22}:  # ENOSYS, EOPNOTSUPP, EINVAL
        return False
    raise OSError(error_number, os.strerror(error_number))


def _replace_managed_path(staged: Path, destination: Path) -> None:
    if not os.path.lexists(destination):
        os.replace(staged, destination)
        return
    if _exchange_paths(staged, destination):
        _remove_managed_path(staged)
        return

    backup = destination.with_name(f".{destination.name}.old-{os.getpid()}")
    _remove_managed_path(backup)
    os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException:
        os.replace(backup, destination)
        raise
    _remove_managed_path(backup)


def _copy_file_reflink(source: str, destination: str) -> str:
    """Create a CoW copy when possible, with a normal copy fallback."""
    ficlone = 0x40049409
    try:
        with open(source, "rb") as source_file, open(destination, "wb") as dest_file:
            fcntl.ioctl(dest_file.fileno(), ficlone, source_file.fileno())
        shutil.copystat(source, destination, follow_symlinks=False)
    except OSError:
        shutil.copy2(source, destination, follow_symlinks=False)
    return destination


def _source_metadata(tool: ProtonTool) -> dict[str, object]:
    files: dict[str, object] = {}
    for name in (
        "version",
        "proton",
        "toolmanifest.vdf",
        "compatibilitytool.vdf",
    ):
        path = tool.canonical_path / name
        try:
            file_stat = path.stat()
            files[name] = [file_stat.st_size, file_stat.st_mtime_ns]
        except OSError:
            files[name] = None
    return {
        "display_name": tool.display_name,
        "source_path": str(tool.canonical_path),
        "version": _read_text(tool.canonical_path / "version").strip(),
        "files": files,
    }


def _copy_is_current(destination: Path, tool: ProtonTool) -> bool:
    destination_launcher = destination / "proton"
    if (
        not destination_launcher.is_file()
        or not os.access(destination_launcher, os.X_OK)
    ):
        return False

    # Proton builds normally carry a version file specifically identifying
    # their contents. If that identifier already matches, preserve the
    # existing managed copy instead of walking and copying the entire build.
    source_version = _read_text(tool.canonical_path / "version").strip()
    destination_version = _read_text(destination / "version").strip()
    if source_version and source_version == destination_version:
        return True

    # Some locally built compatibility tools do not provide a version file.
    # Retain the metadata comparison as a conservative fallback for them.
    metadata_text = _read_text(destination / COPY_METADATA)
    if not metadata_text:
        return False
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError:
        return False
    return metadata == _source_metadata(tool)


class SelectorManager:
    def __init__(
        self,
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
        machine: str | None = None,
    ) -> None:
        self.home = home or Path.home()
        self.env = env or os.environ
        self.base_name = required_base_name(machine)
        self.compatibility_dir = choose_compatibility_directory(self.home, self.env)
        self.tool_path = self.compatibility_dir / SLOT_NAME
        self.selected_dir = self.tool_path / "selected"
        self.fallback_dir = self.tool_path / "fallback"
        self.games_dir = self.tool_path / "games"
        self.game_mappings_path = self.tool_path / GAME_MAPPINGS_CSV
        self.legacy_adapter_path = data_home(self.home, self.env) / "tool"

    def find_base(self, tools: Iterable[ProtonTool]) -> ProtonTool | None:
        exact = [tool for tool in tools if tool.display_name == self.base_name]
        if not exact:
            return None
        exact.sort(key=lambda tool: (not tool.official, str(tool.path)))
        return exact[0]

    def _saved_target(self, filename: str) -> Path | None:
        selection_text = _read_text(self.tool_path / filename)
        lines = selection_text.splitlines()
        if len(lines) < 2:
            return None
        try:
            return Path(lines[1]).resolve(strict=False)
        except OSError:
            return None

    def _saved_name(self, filename: str) -> str:
        selection_file = self.tool_path / filename
        text = _read_text(selection_file)
        if not text:
            return ""
        return text.splitlines()[0].strip()

    def current_target(self) -> Path | None:
        return self._saved_target("selection")

    def current_fallback_target(self) -> Path | None:
        return self._saved_target("fallback-selection")

    def saved_selection_name(self) -> str:
        return self._saved_name("selection")

    def saved_fallback_name(self) -> str:
        return self._saved_name("fallback-selection")

    def selected_copy_available(self) -> bool:
        return (self.selected_dir / "proton").is_file()

    def fallback_copy_available(self) -> bool:
        return (self.fallback_dir / "proton").is_file()

    def game_copy_available(self, mapping: GameMapping) -> bool:
        return (
            self.games_dir
            / mapping.managed_directory
            / "proton"
        ).is_file()

    def _read_game_mappings(self, root: Path | None = None) -> dict[str, GameMapping]:
        mappings_path = (root or self.tool_path) / GAME_MAPPINGS_CSV
        try:
            mappings_file = mappings_path.open(
                "r",
                encoding="utf-8",
                newline="",
            )
        except OSError:
            return {}

        mappings: dict[str, GameMapping] = {}
        with mappings_file:
            for row in csv.DictReader(mappings_file):
                try:
                    game_id = normalize_game_id(row.get("game_id", ""))
                    managed_directory = row.get("managed_directory", "").strip()
                    proton_name = row.get("proton_name", "").strip()
                    source_path = row.get("source_path", "").strip()
                except RuntimeError:
                    continue
                if (
                    not game_id
                    or managed_directory != managed_game_directory(game_id)
                    or not proton_name
                    or not source_path
                ):
                    continue
                mappings[game_id] = GameMapping(
                    game_id=game_id,
                    managed_directory=managed_directory,
                    proton_name=proton_name,
                    source_path=source_path,
                )
        return mappings

    def game_mappings(self) -> dict[str, GameMapping]:
        return self._read_game_mappings()

    def game_mapping(self, game_id: str) -> GameMapping | None:
        normalized = normalize_game_id(game_id)
        if not normalized:
            return None
        return self._read_game_mappings().get(normalized)

    def _write_game_mappings(
        self,
        root: Path,
        mappings: Mapping[str, GameMapping],
    ) -> None:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=GAME_MAPPING_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        for game_id in sorted(mappings, key=str.casefold):
            mapping = mappings[game_id]
            writer.writerow(
                {
                    "game_id": mapping.game_id,
                    "managed_directory": mapping.managed_directory,
                    "proton_name": mapping.proton_name,
                    "source_path": mapping.source_path,
                }
            )
        _atomic_write(root / GAME_MAPPINGS_CSV, output.getvalue())

    def _validate_existing_tool_path(self) -> bool:
        if not os.path.lexists(self.tool_path):
            return False
        if self.tool_path.is_symlink():
            current = self.tool_path.resolve(strict=False)
            expected = self.legacy_adapter_path.resolve(strict=False)
            if current != expected:
                raise RuntimeError(
                    _("unrelated_symlink", path=self.tool_path)
                )
            return True
        if not self.tool_path.is_dir():
            raise RuntimeError(_("not_directory", path=self.tool_path))
        manifest = _read_text(self.tool_path / "compatibilitytool.vdf")
        if '"Proton Selector"' not in manifest:
            raise RuntimeError(_("not_managed", path=self.tool_path))
        return True

    def _validate_source(self, tool: ProtonTool, role: str) -> Path:
        source = tool.canonical_path
        if source == self.tool_path or self.tool_path in source.parents:
            raise RuntimeError(_("source_inside", role=role))
        launcher = source / "proton"
        if not launcher.is_file():
            raise RuntimeError(_("no_launcher", role=role, path=launcher))
        if not os.access(launcher, os.X_OK):
            raise RuntimeError(
                _("launcher_not_executable", role=role, path=launcher)
            )
        return source

    def _copy_tool(
        self,
        tool: ProtonTool,
        destination: Path,
        role: str,
        progress: Callable[[str], None],
        force: bool = False,
    ) -> bool:
        if not force and _copy_is_current(destination, tool):
            progress(_("copy_current", role=role))
            return False

        staged = destination.with_name(f".{destination.name}.next-{os.getpid()}")
        _remove_managed_path(staged)
        progress(_("copying_role", role=role, name=tool.display_name))
        try:
            shutil.copytree(
                tool.canonical_path,
                staged,
                symlinks=True,
                copy_function=_copy_file_reflink,
            )
            _atomic_write(
                staged / COPY_METADATA,
                json.dumps(_source_metadata(tool), indent=2, sort_keys=True) + "\n",
            )
            _replace_managed_path(staged, destination)
        finally:
            _remove_managed_path(staged)
        return True

    def _update_game_mapping(
        self,
        root: Path,
        game_id: str,
        game_tool: ProtonTool,
        progress: Callable[[str], None],
        force: bool = False,
    ) -> None:
        normalized = normalize_game_id(game_id)
        if not normalized:
            return
        directory_name = managed_game_directory(normalized)
        games_dir = root / "games"
        games_dir.mkdir(exist_ok=True)
        self._copy_tool(
            game_tool,
            games_dir / directory_name,
            _("role_game_version", game_id=normalized),
            progress,
            force=force,
        )
        mappings = self._read_game_mappings(root)
        mappings[normalized] = GameMapping(
            game_id=normalized,
            managed_directory=directory_name,
            proton_name=game_tool.display_name,
            source_path=str(game_tool.canonical_path),
        )
        self._write_game_mappings(root, mappings)

    def _write_tool_files(
        self,
        root: Path,
        selected_tool: ProtonTool,
        fallback_tool: ProtonTool,
        base_manifest: Path,
    ) -> None:
        _atomic_write(root / "compatibilitytool.vdf", COMPATIBILITY_TOOL_VDF)
        _atomic_write(
            root / "toolmanifest.vdf",
            base_manifest.read_text(encoding="utf-8", errors="replace"),
        )
        _atomic_write(root / "proton", PROTON_LAUNCHER, mode=0o755)
        _atomic_write(
            root / "selection",
            f"{selected_tool.display_name}\n{selected_tool.canonical_path}\n",
        )
        _atomic_write(
            root / "fallback-selection",
            f"{fallback_tool.display_name}\n{fallback_tool.canonical_path}\n",
        )
        mappings_path = root / GAME_MAPPINGS_CSV
        if not mappings_path.exists():
            self._write_game_mappings(root, {})

    def activate(
        self,
        selected_tool: ProtonTool,
        fallback_tool: ProtonTool,
        base_tool: ProtonTool,
        game_id: str = "",
        game_tool: ProtonTool | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ActivationResult:
        progress = progress or (lambda _message: None)
        selected_path = self._validate_source(
            selected_tool,
            _("role_selected_version"),
        )
        self._validate_source(
            fallback_tool,
            _("role_fallback_version"),
        )
        base_path = self._validate_source(
            base_tool,
            _("role_stable_base"),
        )
        normalized_game_id = normalize_game_id(game_id)
        if normalized_game_id and game_tool is None:
            raise RuntimeError(_("choose_game_version"))
        if game_tool is not None and not normalized_game_id:
            raise RuntimeError(_("enter_game_id"))
        if game_tool is not None:
            self._validate_source(
                game_tool,
                _("role_game_version", game_id=normalized_game_id),
            )
        base_manifest = base_path / "toolmanifest.vdf"

        if not base_manifest.is_file():
            raise RuntimeError(
                _(
                    "missing_manifest",
                    base_name=self.base_name,
                    path=base_manifest,
                )
            )

        self.compatibility_dir.mkdir(parents=True, exist_ok=True)
        already_registered = self._validate_existing_tool_path()

        if self.tool_path.is_dir() and not self.tool_path.is_symlink():
            self._write_tool_files(
                self.tool_path,
                selected_tool,
                fallback_tool,
                base_manifest,
            )
            self._copy_tool(
                selected_tool,
                self.selected_dir,
                _("role_selected_version"),
                progress,
            )
            self._copy_tool(
                fallback_tool,
                self.fallback_dir,
                _("role_fallback_version"),
                progress,
            )
            if normalized_game_id and game_tool:
                self._update_game_mapping(
                    self.tool_path,
                    normalized_game_id,
                    game_tool,
                    progress,
                )
        else:
            staged_tool = self.compatibility_dir / f".{SLOT_NAME}.new-{os.getpid()}"
            _remove_managed_path(staged_tool)
            staged_tool.mkdir()
            try:
                self._write_tool_files(
                    staged_tool,
                    selected_tool,
                    fallback_tool,
                    base_manifest,
                )
                self._copy_tool(
                    selected_tool,
                    staged_tool / "selected",
                    _("role_selected_version"),
                    progress,
                    force=True,
                )
                self._copy_tool(
                    fallback_tool,
                    staged_tool / "fallback",
                    _("role_fallback_version"),
                    progress,
                    force=True,
                )
                if normalized_game_id and game_tool:
                    self._update_game_mapping(
                        staged_tool,
                        normalized_game_id,
                        game_tool,
                        progress,
                        force=True,
                    )
                progress(_("installing"))
                _replace_managed_path(staged_tool, self.tool_path)
            finally:
                _remove_managed_path(staged_tool)

        progress(_("ready"))
        return ActivationResult(
            first_activation=not already_registered,
            tool_path=self.tool_path,
            selected_path=selected_path,
        )


def _resolve_cli_tool(
    selector: str,
    tools: Iterable[ProtonTool],
) -> ProtonTool:
    available = list(tools)
    candidate_path = Path(selector).expanduser()
    if candidate_path.exists() or os.sep in selector:
        try:
            canonical_candidate = candidate_path.resolve()
        except OSError:
            canonical_candidate = candidate_path.absolute()
        path_matches = [
            tool
            for tool in available
            if tool.canonical_path == canonical_candidate
        ]
        if len(path_matches) == 1:
            return path_matches[0]

    exact_matches = [
        tool for tool in available if tool.display_name == selector
    ]
    matches = exact_matches or [
        tool
        for tool in available
        if tool.display_name.casefold() == selector.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        paths = "\n".join(f"  {tool.canonical_path}" for tool in matches)
        raise RuntimeError(
            f'Proton version "{selector}" is ambiguous. Use one of these paths:\n'
            f"{paths}"
        )
    raise RuntimeError(
        f'No installed Proton version matches "{selector}". '
        "Use --list-versions to see available versions."
    )


def _tool_for_saved_path(
    saved_path: Path | None,
    tools: Iterable[ProtonTool],
) -> ProtonTool | None:
    if saved_path is None:
        return None
    return next(
        (
            tool
            for tool in tools
            if tool.canonical_path == saved_path.resolve(strict=False)
        ),
        None,
    )


def _print_cli_status(manager: SelectorManager) -> None:
    active_name = manager.saved_selection_name() or _("none")
    if active_name != _("none") and not manager.selected_copy_available():
        active_name = _("files_missing", name=active_name)
    fallback_name = manager.saved_fallback_name() or _("none")
    if fallback_name != _("none") and not manager.fallback_copy_available():
        fallback_name = _("files_missing", name=fallback_name)
    print(_("status_active", version=active_name))
    print(_("status_fallback", version=fallback_name))
    for game_id, mapping in sorted(manager.game_mappings().items()):
        name = mapping.proton_name
        if not manager.game_copy_available(mapping):
            name = _("files_missing", name=name)
        print(_("status_game", game_id=game_id, version=name))


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proton-selector",
        description=(
            "Select the Active, Fallback, or per-game Proton version without "
            "opening the graphical interface."
        ),
    )
    parser.add_argument(
        "--list-versions",
        action="store_true",
        help="list installed Proton versions and their paths",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show the current Active, Fallback, and game mappings",
    )
    parser.add_argument(
        "--active",
        metavar="VERSION",
        help="set the Active Version by exact display name or path",
    )
    parser.add_argument(
        "--fallback",
        metavar="VERSION",
        help="set the Fallback Version by exact display name or path",
    )
    parser.add_argument(
        "--game-id",
        metavar="ID",
        help="Steam game ID or UMU ID used with --game",
    )
    parser.add_argument(
        "--game",
        metavar="VERSION",
        help="set the Game Version by exact display name or path",
    )
    parser.add_argument(
        "--language",
        choices=("system", *CATALOGS),
        help="save the UI language or restore system-language selection",
    )
    return parser


def _run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if bool(args.game_id) != bool(args.game):
        parser.error("--game-id and --game must be used together.")

    if args.language:
        if args.language == "system":
            _.use_system_language()
        else:
            _.set_language(args.language)
        save_language_preference(args.language)
        selected_language = LANGUAGE_NAMES[_.language]
        print(f'{_("language")}: {selected_language}')

    manager = SelectorManager()
    tools = scan_proton_tools()

    if args.list_versions:
        for tool in tools:
            print(f"{tool.display_name}\t{tool.canonical_path}")

    if args.status:
        _print_cli_status(manager)

    changing_versions = bool(args.active or args.fallback or args.game)
    if not changing_versions:
        if args.list_versions or args.status or args.language:
            return 0
        parser.error("No CLI action was requested.")

    base_tool = manager.find_base(tools)
    if base_tool is None:
        raise RuntimeError(
            _("not_installed", name=manager.base_name)
        )

    selected_tool = (
        _resolve_cli_tool(args.active, tools)
        if args.active
        else _tool_for_saved_path(manager.current_target(), tools)
    )
    fallback_tool = (
        _resolve_cli_tool(args.fallback, tools)
        if args.fallback
        else _tool_for_saved_path(manager.current_fallback_target(), tools)
    )
    if selected_tool is None:
        if manager.current_target() is not None:
            raise RuntimeError(
                "The saved Active Version source is unavailable; specify --active."
            )
        selected_tool = base_tool
    if fallback_tool is None:
        if manager.current_fallback_target() is not None:
            raise RuntimeError(
                "The saved Fallback Version source is unavailable; "
                "specify --fallback."
            )
        fallback_tool = base_tool

    game_id = normalize_game_id(args.game_id or "")
    game_tool = _resolve_cli_tool(args.game, tools) if args.game else None
    result = manager.activate(
        selected_tool,
        fallback_tool,
        base_tool,
        game_id=game_id,
        game_tool=game_tool,
        progress=print,
    )
    if game_id and game_tool:
        print(
            _(
                "game_success",
                game_id=game_id,
                name=game_tool.display_name,
            )
        )
    else:
        print(_("selector_success", name=selected_tool.display_name))
    if result.first_activation:
        print(_("restart_first"))
    return 0


class ProtonSelectorWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(
            application=application,
            title=APP_NAME,
            default_width=820,
            default_height=650,
        )
        self.manager = SelectorManager()
        self.tools: list[ProtonTool] = []
        self.base_tool: ProtonTool | None = None
        self.copying = False
        self.pulse_source = 0
        environment_override = os.environ.get("PROTON_SELECTOR_LANGUAGE", "")
        if environment_override:
            self.language_preference = _.language
        else:
            self.language_preference = saved_language_preference()
            if self.language_preference == "system":
                _.use_system_language()
            else:
                _.set_language(self.language_preference)

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        overlay = Gtk.Overlay()
        self.set_child(overlay)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        content_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        content_scroller.set_child(outer)
        overlay.set_child(content_scroller)

        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        title = Gtk.Label(label=APP_NAME, xalign=0, hexpand=True)
        title.add_css_class("title-1")
        title_row.append(title)

        language_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            valign=Gtk.Align.CENTER,
        )
        language_box.append(Gtk.Label(label=_("language")))
        self.language_option_codes = ["system", *LANGUAGE_NAMES]
        language_model = Gtk.StringList()
        language_model.splice(
            0,
            0,
            [
                _("system_default"),
                *(LANGUAGE_NAMES[code] for code in LANGUAGE_NAMES),
            ],
        )
        self.language_dropdown = Gtk.DropDown(model=language_model)
        selected_language_index = (
            self.language_option_codes.index(self.language_preference)
            if self.language_preference in self.language_option_codes
            else 0
        )
        self.language_dropdown.set_selected(selected_language_index)
        self.language_dropdown.connect(
            "notify::selected",
            self._language_changed,
        )
        language_box.append(self.language_dropdown)
        title_row.append(language_box)
        outer.append(title_row)

        subtitle = Gtk.Label(
            label=_("instruction"),
            xalign=0,
            wrap=True,
        )
        outer.append(subtitle)

        version_label = Gtk.Label(label=_("active_version"), xalign=0)
        version_label.add_css_class("heading")
        outer.append(version_label)

        self.version_model = Gtk.StringList()
        self.version_dropdown = Gtk.DropDown(
            model=self.version_model,
            hexpand=True,
        )
        if hasattr(self.version_dropdown, "set_enable_search"):
            self.version_dropdown.set_enable_search(True)
        self.version_dropdown.connect("notify::selected", self._selection_changed)
        outer.append(self.version_dropdown)

        fallback_label = Gtk.Label(label=_("fallback_version"), xalign=0)
        fallback_label.add_css_class("heading")
        outer.append(fallback_label)

        self.fallback_dropdown = Gtk.DropDown(
            model=self.version_model,
            hexpand=True,
        )
        if hasattr(self.fallback_dropdown, "set_enable_search"):
            self.fallback_dropdown.set_enable_search(True)
        self.fallback_dropdown.connect("notify::selected", self._selection_changed)
        outer.append(self.fallback_dropdown)

        game_id_label = Gtk.Label(label=_("game_id_optional"), xalign=0)
        game_id_label.add_css_class("heading")
        outer.append(game_id_label)

        self.game_id_entry = Gtk.Entry(
            placeholder_text=_("game_id_placeholder"),
            hexpand=True,
        )
        self.game_id_entry.connect("changed", self._game_id_changed)
        outer.append(self.game_id_entry)

        game_label = Gtk.Label(label=_("game_version"), xalign=0)
        game_label.add_css_class("heading")
        outer.append(game_label)

        self.game_dropdown = Gtk.DropDown(
            model=self.version_model,
            hexpand=True,
            sensitive=False,
        )
        if hasattr(self.game_dropdown, "set_enable_search"):
            self.game_dropdown.set_enable_search(True)
        self.game_dropdown.connect("notify::selected", self._selection_changed)
        outer.append(self.game_dropdown)

        details = Gtk.Grid(
            column_spacing=12,
            row_spacing=6,
            margin_top=4,
            margin_bottom=4,
        )
        details.attach(Gtk.Label(label=_("runtime_appid"), xalign=0), 0, 0, 1, 1)
        details.attach(Gtk.Label(label=_("location"), xalign=0), 0, 1, 1, 1)
        details.attach(Gtk.Label(label=_("source"), xalign=0), 0, 2, 1, 1)

        self.runtime_label = Gtk.Label(xalign=0, selectable=True)
        self.location_label = Gtk.Label(
            xalign=0,
            hexpand=True,
            selectable=True,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
        )
        self.source_label = Gtk.Label(
            xalign=0,
            hexpand=True,
            selectable=True,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
        )
        self.runtime_label.add_css_class("dim-label")
        self.location_label.add_css_class("dim-label")
        self.source_label.add_css_class("dim-label")
        details.attach(self.runtime_label, 1, 0, 1, 1)
        details.attach(self.location_label, 1, 1, 1, 1)
        details.attach(self.source_label, 1, 2, 1, 1)
        outer.append(details)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.refresh_button = Gtk.Button(label=_("refresh"))
        self.refresh_button.connect("clicked", lambda _button: self.refresh())
        button_row.append(self.refresh_button)

        spacer = Gtk.Box(hexpand=True)
        button_row.append(spacer)

        self.activate_button = Gtk.Button(label=_("use_selected"))
        self.activate_button.add_css_class("suggested-action")
        self.activate_button.set_sensitive(False)
        self.activate_button.connect("clicked", lambda _button: self.activate_selected())
        button_row.append(self.activate_button)
        outer.append(button_row)

        self.copy_progress = Gtk.ProgressBar(show_text=True, visible=False)
        outer.append(self.copy_progress)

        outer.append(Gtk.Separator())
        self.status_label = Gtk.Label(
            label=_("scanning"),
            xalign=0,
            wrap=True,
            selectable=True,
        )
        outer.append(self.status_label)

        self.notification_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=250,
            reveal_child=False,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.END,
            hexpand=True,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self.notification_frame = Gtk.Frame()
        self.notification_frame.add_css_class("osd")
        notification_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        notification_text = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            hexpand=True,
        )
        self.notification_title = Gtk.Label(xalign=0, wrap=True)
        self.notification_title.add_css_class("heading")
        self.notification_body = Gtk.Label(xalign=0, wrap=True)
        notification_text.append(self.notification_title)
        notification_text.append(self.notification_body)
        notification_row.append(notification_text)

        self.notification_close = Gtk.Button(
            label=_("close"),
            valign=Gtk.Align.CENTER,
        )
        self.notification_close.connect("clicked", self._close_notification)
        notification_row.append(self.notification_close)
        self.notification_frame.set_child(notification_row)
        self.notification_revealer.set_child(self.notification_frame)
        overlay.add_overlay(self.notification_revealer)

    def _language_changed(
        self,
        dropdown: Gtk.DropDown,
        _parameter: object,
    ) -> None:
        index = dropdown.get_selected()
        if index < 0 or index >= len(self.language_option_codes):
            return
        preference = self.language_option_codes[index]
        if preference == self.language_preference:
            return

        active_tool = self._selected_tool()
        fallback_tool = self._selected_fallback_tool()
        game_tool = self._selected_game_tool()
        active_path = str(active_tool.canonical_path) if active_tool else ""
        fallback_path = (
            str(fallback_tool.canonical_path) if fallback_tool else ""
        )
        game_path = str(game_tool.canonical_path) if game_tool else ""
        game_id = self.game_id_entry.get_text()

        if preference == "system":
            _.use_system_language()
        else:
            _.set_language(preference)
        save_language_preference(preference)
        self.language_preference = preference

        self._build_ui()
        self.refresh()
        for dropdown_widget, path in (
            (self.version_dropdown, active_path),
            (self.fallback_dropdown, fallback_path),
        ):
            selected_index = self._tool_index_for_path(path)
            if selected_index >= 0:
                dropdown_widget.set_selected(selected_index)
        self.game_id_entry.set_text(game_id)
        selected_game_index = self._tool_index_for_path(game_path)
        if selected_game_index >= 0:
            self.game_dropdown.set_selected(selected_game_index)
        self.refresh()

    def _selected_tool(self) -> ProtonTool | None:
        index = self.version_dropdown.get_selected()
        if index < 0 or index >= len(self.tools):
            return None
        return self.tools[index]

    def _selected_fallback_tool(self) -> ProtonTool | None:
        index = self.fallback_dropdown.get_selected()
        if index < 0 or index >= len(self.tools):
            return None
        return self.tools[index]

    def _selected_game_tool(self) -> ProtonTool | None:
        index = self.game_dropdown.get_selected()
        if index < 0 or index >= len(self.tools):
            return None
        return self.tools[index]

    def _tool_index_for_path(self, path: str) -> int:
        if not path:
            return -1
        return next(
            (
                index
                for index, tool in enumerate(self.tools)
                if str(tool.canonical_path) == path
            ),
            -1,
        )

    def _game_id_changed(self, _entry: Gtk.Entry) -> None:
        raw_game_id = self.game_id_entry.get_text()
        try:
            game_id = normalize_game_id(raw_game_id)
        except RuntimeError as error:
            self.game_id_entry.add_css_class("error")
            self.game_id_entry.set_tooltip_text(str(error))
            self.game_dropdown.set_sensitive(False)
            self._selection_changed(self.game_dropdown, None)
            return

        self.game_id_entry.remove_css_class("error")
        self.game_id_entry.set_tooltip_text(None)
        self.game_dropdown.set_sensitive(bool(game_id) and not self.copying)
        if game_id:
            mapping = self.manager.game_mapping(game_id)
            mapped_index = self._tool_index_for_path(
                mapping.source_path if mapping else ""
            )
            if mapped_index < 0:
                mapped_index = self.version_dropdown.get_selected()
            if 0 <= mapped_index < len(self.tools):
                self.game_dropdown.set_selected(mapped_index)
        self._selection_changed(self.game_dropdown, None)

    def _selection_changed(
        self,
        _dropdown: Gtk.DropDown,
        _parameter: object,
    ) -> None:
        tool = self._selected_tool()
        fallback_tool = self._selected_fallback_tool()
        raw_game_id = self.game_id_entry.get_text()
        try:
            game_id = normalize_game_id(raw_game_id)
            valid_game_id = True
        except RuntimeError:
            game_id = ""
            valid_game_id = False
        game_tool = self._selected_game_tool() if game_id else None
        self.activate_button.set_sensitive(
            not self.copying
            and tool is not None
            and fallback_tool is not None
            and self.base_tool is not None
            and valid_game_id
            and (not game_id or game_tool is not None)
        )
        if _dropdown is not self.version_dropdown:
            return
        if tool is None:
            self.runtime_label.set_text("—")
            self.location_label.set_text("—")
            self.source_label.set_text("—")
            return
        self.runtime_label.set_text(tool.runtime_appid or "—")
        self.location_label.set_text(str(tool.path))
        self.location_label.set_tooltip_text(str(tool.path))
        self.source_label.set_text(tool.source)
        self.source_label.set_tooltip_text(tool.source)

    def refresh(self) -> None:
        if self.copying:
            return
        previous_path = ""
        previous_fallback_path = ""
        previous_game_path = ""
        selected = self._selected_tool()
        selected_fallback = self._selected_fallback_tool()
        selected_game = self._selected_game_tool()
        if selected:
            previous_path = str(selected.canonical_path)
        if selected_fallback:
            previous_fallback_path = str(selected_fallback.canonical_path)
        if selected_game:
            previous_game_path = str(selected_game.canonical_path)

        self.tools = scan_proton_tools()
        self.base_tool = self.manager.find_base(self.tools)
        current = self.manager.current_target()
        current_fallback = self.manager.current_fallback_target()
        current_key = str(current) if current else ""
        current_fallback_key = str(current_fallback) if current_fallback else ""

        selected_index = -1
        fallback_index = -1
        game_index = -1
        name_counts = Counter(tool.display_name for tool in self.tools)
        dropdown_labels: list[str] = []
        for index, tool in enumerate(self.tools):
            label = tool.display_name
            if name_counts[tool.display_name] > 1:
                label = f"{label} — {tool.path}"
            dropdown_labels.append(label)
            canonical = str(tool.canonical_path)
            if canonical == previous_path or (not previous_path and canonical == current_key):
                selected_index = index
            if canonical == previous_fallback_path or (
                not previous_fallback_path and canonical == current_fallback_key
            ):
                fallback_index = index
            if canonical == previous_game_path:
                game_index = index
        self.version_model.splice(
            0,
            self.version_model.get_n_items(),
            dropdown_labels,
        )

        # Make the primary action immediately available. Prefer the previous or
        # active build, then Valve's stable base, then the first discovered tool.
        if selected_index < 0 and self.base_tool:
            base_path = str(self.base_tool.canonical_path)
            selected_index = next(
                (
                    index
                    for index, tool in enumerate(self.tools)
                    if str(tool.canonical_path) == base_path
                ),
                -1,
            )
        if selected_index < 0 and self.tools:
            selected_index = 0
        if selected_index >= 0:
            self.version_dropdown.set_selected(selected_index)

        if fallback_index < 0 and self.base_tool:
            base_path = str(self.base_tool.canonical_path)
            fallback_index = next(
                (
                    index
                    for index, tool in enumerate(self.tools)
                    if str(tool.canonical_path) == base_path
                ),
                -1,
            )
        if fallback_index < 0 and self.tools:
            fallback_index = 0
        if fallback_index >= 0:
            self.fallback_dropdown.set_selected(fallback_index)

        raw_game_id = self.game_id_entry.get_text()
        try:
            game_id = normalize_game_id(raw_game_id)
        except RuntimeError:
            game_id = ""
        if game_id and not previous_game_path:
            mapping = self.manager.game_mapping(game_id)
            game_index = self._tool_index_for_path(
                mapping.source_path if mapping else ""
            )
        if game_index < 0:
            game_index = selected_index
        if game_index >= 0:
            self.game_dropdown.set_selected(game_index)
        self.game_dropdown.set_sensitive(bool(game_id) and not self.copying)

        game_mapping = self.manager.game_mapping(game_id) if game_id else None
        if game_mapping and self.manager.game_copy_available(game_mapping):
            active = game_mapping.proton_name
        elif game_mapping:
            active = _(
                "files_missing",
                name=game_mapping.proton_name,
            )
        else:
            saved_name = self.manager.saved_selection_name()
            if saved_name and self.manager.selected_copy_available():
                active = saved_name
            elif saved_name:
                active = _("files_missing", name=saved_name)
            else:
                active = _("none")

        saved_fallback_name = self.manager.saved_fallback_name()
        if saved_fallback_name and self.manager.fallback_copy_available():
            fallback = saved_fallback_name
        elif saved_fallback_name:
            fallback = _("files_missing", name=saved_fallback_name)
        elif self.base_tool:
            fallback = self.base_tool.display_name
        else:
            fallback = _("not_installed", name=self.manager.base_name)
        self.status_label.set_text(
            f'{_("status_active", version=active)}\n'
            f'{_("status_fallback", version=fallback)}'
        )
        self._selection_changed(self.version_dropdown, None)

    def _notify(
        self,
        title: str,
        body: str,
        error: bool = False,
    ) -> None:
        self.notification_title.set_text(
            _("error_title", app_name=title) if error else title
        )
        self.notification_body.set_text(body)
        self.notification_frame.remove_css_class("error")
        if error:
            self.notification_frame.add_css_class("error")
        self.notification_revealer.set_reveal_child(True)
        self.notification_close.grab_focus()

    def _close_notification(self, _button: Gtk.Button) -> None:
        self.notification_revealer.set_reveal_child(False)
        if self.activate_button.get_sensitive():
            self.activate_button.grab_focus()
        else:
            self.game_id_entry.grab_focus()

    def activate_selected(self) -> None:
        selected_tool = self._selected_tool()
        fallback_tool = self._selected_fallback_tool()
        raw_game_id = self.game_id_entry.get_text()
        try:
            game_id = normalize_game_id(raw_game_id)
        except RuntimeError as error:
            self._notify(APP_NAME, str(error), error=True)
            return
        game_tool = self._selected_game_tool() if game_id else None
        if (
            self.copying
            or selected_tool is None
            or fallback_tool is None
            or (game_id and game_tool is None)
            or not self.base_tool
        ):
            return
        base_tool = self.base_tool
        self.copying = True
        self.notification_revealer.set_reveal_child(False)
        self.version_dropdown.set_sensitive(False)
        self.fallback_dropdown.set_sensitive(False)
        self.game_id_entry.set_sensitive(False)
        self.game_dropdown.set_sensitive(False)
        self.refresh_button.set_sensitive(False)
        self.activate_button.set_sensitive(False)
        self.activate_button.set_label(_("copying"))
        self.copy_progress.set_text(_("preparing"))
        self.copy_progress.set_visible(True)
        self.pulse_source = GLib.timeout_add(100, self._pulse_copy_progress)

        worker = threading.Thread(
            target=self._activation_worker,
            args=(selected_tool, fallback_tool, base_tool, game_id, game_tool),
            daemon=True,
        )
        worker.start()

    def _pulse_copy_progress(self) -> bool:
        if not self.copying:
            return False
        self.copy_progress.pulse()
        return True

    def _set_copy_progress(self, message: str) -> bool:
        self.copy_progress.set_text(message)
        return False

    def _activation_worker(
        self,
        selected_tool: ProtonTool,
        fallback_tool: ProtonTool,
        base_tool: ProtonTool,
        game_id: str,
        game_tool: ProtonTool | None,
    ) -> None:
        result: ActivationResult | None = None
        error: Exception | None = None
        try:
            result = self.manager.activate(
                selected_tool,
                fallback_tool,
                base_tool,
                game_id=game_id,
                game_tool=game_tool,
                progress=lambda message: GLib.idle_add(
                    self._set_copy_progress,
                    message,
                ),
            )
        except Exception as caught:
            error = caught
        GLib.idle_add(
            self._activation_finished,
            selected_tool,
            game_id,
            game_tool,
            result,
            error,
        )

    def _activation_finished(
        self,
        selected_tool: ProtonTool,
        game_id: str,
        game_tool: ProtonTool | None,
        result: ActivationResult | None,
        error: Exception | None,
    ) -> bool:
        self.copying = False
        if self.pulse_source:
            GLib.source_remove(self.pulse_source)
            self.pulse_source = 0
        self.copy_progress.set_visible(False)
        self.activate_button.set_label(_("use_selected"))
        self.version_dropdown.set_sensitive(True)
        self.fallback_dropdown.set_sensitive(True)
        self.game_id_entry.set_sensitive(True)
        self.refresh_button.set_sensitive(True)
        self.refresh()

        if error is not None:
            self._notify(APP_NAME, str(error), error=True)
            return False
        if result is None:
            self._notify(APP_NAME, _("operation_incomplete"), error=True)
            return False
        if game_id and game_tool:
            success_text = (
                _(
                    "game_success",
                    game_id=game_id,
                    name=game_tool.display_name,
                )
                + "\n\n"
                + _("game_copies_ready")
            )
        else:
            success_text = (
                _("selector_success", name=selected_tool.display_name)
                + "\n\n"
                + _("default_copies_ready")
            )
        if result.first_activation:
            self._notify(
                APP_NAME,
                (
                    f"{success_text}\n\n"
                    + _("restart_first")
                ),
            )
        else:
            self._notify(
                APP_NAME,
                success_text,
            )
        return False


class ProtonSelectorApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="io.github.protonselector.ProtonSelector")

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = ProtonSelectorWindow(self)
        window.present()


def main() -> int:
    if len(sys.argv) > 1:
        if not os.environ.get("PROTON_SELECTOR_LANGUAGE"):
            preference = saved_language_preference()
            if preference == "system":
                _.use_system_language()
            else:
                _.set_language(preference)
        parser = _build_cli_parser()
        arguments = parser.parse_args(sys.argv[1:])
        try:
            return _run_cli(arguments, parser)
        except RuntimeError as error:
            parser.exit(1, f"{parser.prog}: error: {error}\n")
    application = ProtonSelectorApplication()
    return application.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
