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
import tarfile
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping

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
    return 0
}

apply_proton_environment_file()
{
    environment_file=$1
    [ -f "$environment_file" ] || return 0
    while IFS= read -r entry || [ -n "$entry" ]; do
        case "$entry" in
            *=*) variable=${entry%%=*}; value=${entry#*=} ;;
            *)   variable=$entry; value=1 ;;
        esac
        case "$variable" in
            ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) continue ;;
        esac
        if [ -n "$variable" ]; then
            export "$variable=$value"
        fi
    done < "$environment_file"
}

launch_game_id=""
for candidate_id in \
    "${SteamGameId-}" \
    "${GAMEID-}" \
    "${UMU_ID-}" \
    "${STEAM_COMPAT_APP_ID-}" \
    "${SteamAppId-}"
do
    [ -n "$candidate_id" ] || continue
    case "$candidate_id" in
        *[!a-zA-Z0-9._:-]*) continue ;;
    esac
    if [ -z "$launch_game_id" ]; then
        launch_game_id=$candidate_id
    fi
    if lookup_game_copy "$candidate_id"; then
        launch_game_id=$candidate_id
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

apply_proton_environment_file "$selector_dir/proton-environment"
if [ -n "$launch_game_id" ]; then
    PROTON_SELECTOR_GAME_ID="$launch_game_id"
    export PROTON_SELECTOR_GAME_ID
    apply_proton_environment_file \
        "$selector_dir/game-environment/$launch_game_id.env"
fi

PROTON_SELECTOR_TARGET="$selected"
export PROTON_SELECTOR_TARGET
exec "$selected/proton" "$@"
'''

PROTON_ENVIRONMENT_VARIABLE_PATTERN = re.compile(
    r"\b(?:(?:PROTON|DXVK|VKD3D)_[A-Z][A-Z0-9_]*|WINE[A-Z0-9_]+)\b"
)
ENVIRONMENT_VARIABLE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SUPPORTED_PROTON_ENVIRONMENT_VARIABLES = frozenset(
    {"DXVK_CONFIG", "VKD3D_CONFIG", "HOST_LC_ALL"}
)
PROTON_BOOLEAN_ENVIRONMENT_PATTERN = re.compile(
    r"check_environment\(\s*['\"](PROTON_[A-Z][A-Z0-9_]*)['\"]"
)
WINELAND_RELEASE_API = (
    "https://api.github.com/repos/nanomatters/proton-cachyos/releases/latest"
)
WINELAND_INSTALLS = {
    "normal": ("Proton Wineland", "Proton Wineland"),
    "v3": ("Proton Wineland v3", "Proton Wineland v3"),
    "wow64": ("Proton Wineland WoW64", "Proton Wineland WoW64"),
}
WINELAND_MANAGED_MARKER = ".proton-selector-wineland.json"


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


@dataclass(frozen=True)
class InstalledGame:
    game_id: str
    name: str


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


def scan_installed_games(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[InstalledGame]:
    home = home or Path.home()
    env = env or os.environ
    steam_roots = discover_steam_roots(home, env)
    library_roots = discover_library_roots(steam_roots)
    games: dict[str, InstalledGame] = {}

    for library_root in library_roots:
        steamapps = library_root / "steamapps"
        for manifest in steamapps.glob("appmanifest_*.acf"):
            text = _read_text(manifest)
            game_id = _vdf_value(text, "appid")
            name = _vdf_value(text, "name")
            install_dir = _vdf_value(text, "installdir")
            if not game_id.isdigit() or not name or not install_dir:
                continue
            tool_manifest = steamapps / "common" / install_dir / "toolmanifest.vdf"
            if tool_manifest.is_file():
                continue
            games.setdefault(game_id, InstalledGame(game_id, name))

    return sorted(games.values(), key=lambda game: (game.name.casefold(), game.game_id))


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


def proton_wineland_variant(tool: ProtonTool) -> str | None:
    for variant, (_display_name, directory_name) in WINELAND_INSTALLS.items():
        if tool.path.name == directory_name:
            return variant
    for name in (tool.path.name, tool.display_name):
        if not name.startswith("proton-wineland-"):
            continue
        if name.endswith("_v3"):
            return "v3"
        if name.endswith("_wow64"):
            return "wow64"
        if re.fullmatch(r"proton-wineland-.+-x86_64", name):
            return "normal"
    return None


def installed_proton_wineland_variants(compatibility_dir: Path) -> set[str]:
    variants = set()
    for variant, (_display_name, directory_name) in WINELAND_INSTALLS.items():
        install_path = compatibility_dir / directory_name
        marker_path = install_path / WINELAND_MANAGED_MARKER
        if install_path.is_symlink() or marker_path.is_symlink() or not marker_path.is_file():
            continue
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if marker.get("variant") == variant:
            variants.add(variant)

    variants.update(
        variant
        for tool in scan_custom_tools([compatibility_dir])
        if (variant := proton_wineland_variant(tool)) is not None
    )
    return variants


def proton_environment_variables(proton_path: Path) -> tuple[str, ...]:
    source = _read_text(proton_path / "proton")
    variables = set(PROTON_ENVIRONMENT_VARIABLE_PATTERN.findall(source))
    variables.update(SUPPORTED_PROTON_ENVIRONMENT_VARIABLES)
    return tuple(sorted(variables))


def proton_environment_variable_types(proton_path: Path) -> dict[str, str]:
    source = _read_text(proton_path / "proton")
    boolean_variables = set(PROTON_BOOLEAN_ENVIRONMENT_PATTERN.findall(source))
    return {
        name: "boolean" if name in boolean_variables else "string"
        for name in proton_environment_variables(proton_path)
    }


def _safe_extract_tar(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    def normalize_link(base: tuple[str, ...], link_name: str) -> tuple[str, ...]:
        link = PurePosixPath(link_name)
        if link.is_absolute():
            raise RuntimeError("The Wineland archive contains an absolute link.")
        parts = list(base)
        for part in link.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise RuntimeError("The Wineland archive contains an unsafe link.")
                parts.pop()
            else:
                parts.append(part)
        if not parts:
            raise RuntimeError("The Wineland archive contains an unsafe link.")
        return tuple(parts)

    with tarfile.open(archive_path, mode="r:xz") as archive:
        entries = []
        path_kinds: dict[tuple[str, ...], str] = {}
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("The Wineland archive contains an unsafe path.")
            parts = tuple(part for part in relative.parts if part not in {"", "."})
            if not parts and member.isdir():
                continue
            if not parts:
                raise RuntimeError("The Wineland archive contains an unsafe path.")
            if member.isdir():
                kind = "directory"
            elif member.isfile():
                kind = "file"
            elif member.issym():
                kind = "symlink"
            elif member.islnk():
                kind = "hardlink"
            else:
                raise RuntimeError("The Wineland archive contains an unsupported file type.")
            if parts in path_kinds and not (
                kind == path_kinds[parts] == "directory"
            ):
                raise RuntimeError("The Wineland archive contains duplicate paths.")
            path_kinds[parts] = kind
            link_parts = None
            if member.issym():
                link_parts = normalize_link(parts[:-1], member.linkname)
            elif member.islnk():
                link_parts = normalize_link((), member.linkname)
            entries.append((member, parts, kind, link_parts))

        for parts in path_kinds:
            for length in range(1, len(parts)):
                ancestor = parts[:length]
                if ancestor in path_kinds and path_kinds[ancestor] != "directory":
                    raise RuntimeError("The Wineland archive contains a path collision.")

        directory_modes = []
        for member, parts, kind, _link_parts in entries:
            if kind != "directory":
                continue
            target = root.joinpath(*parts)
            target.mkdir(parents=True, exist_ok=True)
            directory_modes.append((target, member.mode & 0o777))

        regular_files = [entry for entry in entries if entry[2] == "file"]
        total_size = sum(member.size for member, _parts, _kind, _link in regular_files)
        if shutil.disk_usage(root).free < total_size + 64 * 1024 * 1024:
            raise RuntimeError("There is not enough free space to extract Proton Wineland.")

        for member, parts, kind, _link_parts in regular_files:
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("The Wineland archive contains an unreadable file.")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)

        hardlinks = [entry for entry in entries if entry[2] == "hardlink"]
        while hardlinks:
            remaining = []
            created = False
            for member, parts, _kind, link_parts in hardlinks:
                target = root.joinpath(*parts)
                source = root.joinpath(*(link_parts or ()))
                if not source.is_file() or source.is_symlink():
                    remaining.append((member, parts, "hardlink", link_parts))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
                created = True
            if remaining and not created:
                raise RuntimeError("The Wineland archive contains an invalid hard link.")
            hardlinks = remaining

        for _member, parts, _kind, link_parts in entries:
            if _kind != "symlink":
                continue
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(_member.linkname, target)

        for directory, mode in reversed(directory_modes):
            directory.chmod(mode)


def install_proton_wineland(
    compatibility_dir: Path,
    variant: str,
    progress: Callable[[str], None] | None = None,
    machine: str | None = None,
) -> str | None:
    if variant not in WINELAND_INSTALLS:
        raise RuntimeError(f"Unsupported Proton Wineland variant: {variant}")
    if (machine or platform.machine()).lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Proton Wineland releases are currently available for x86_64 only.")

    display_name, directory_name = WINELAND_INSTALLS[variant]
    compatibility_dir.mkdir(parents=True, exist_ok=True)
    destination = compatibility_dir / directory_name

    report = progress or (lambda _message: None)
    report("Checking the latest Proton Wineland release...")
    request = urllib.request.Request(
        WINELAND_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Proton Selector",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)

    tag = release.get("tag_name", "")
    if not re.fullmatch(r"wineland-[A-Za-z0-9.-]+", tag):
        raise RuntimeError("GitHub returned an invalid Proton Wineland release tag.")
    suffix = {"normal": "", "v3": "_v3", "wow64": "_wow64"}[variant]
    asset_name = f"proton-{tag}-x86_64{suffix}.tar.xz"
    asset = next(
        (entry for entry in release.get("assets", []) if entry.get("name") == asset_name),
        None,
    )
    if asset is None:
        raise RuntimeError(f"The latest release does not include {asset_name}.")
    if os.path.lexists(destination):
        marker_path = destination / WINELAND_MANAGED_MARKER
        if destination.is_symlink() or not destination.is_dir() or marker_path.is_symlink():
            raise RuntimeError(f"Refusing to replace unmanaged path: {destination}")
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = {}
        if marker.get("variant") == variant and marker.get("tag") == tag:
            report(f"Proton Wineland {variant} {tag} is already installed.")
            return None
        if marker and marker.get("variant") != variant:
            raise RuntimeError(f"Refusing to replace a different Wineland install: {destination}")
        if not marker:
            if any(
                tool.path.name == asset_name.removesuffix(".tar.xz")
                and tool.display_name == asset_name.removesuffix(".tar.xz")
                for tool in scan_custom_tools([compatibility_dir])
            ):
                report(f"Proton Wineland {variant} {tag} is already installed.")
                return None
            raise RuntimeError(f"Refusing to replace unmanaged path: {destination}")

    archive_stem = asset_name.removesuffix(".tar.xz")
    if any(
        tool.path.name == archive_stem and tool.display_name == archive_stem
        for tool in scan_custom_tools([compatibility_dir])
    ):
        report(f"Proton Wineland {variant} {tag} is already installed.")
        return None

    asset_url = asset.get("browser_download_url", "")
    parsed_url = urllib.parse.urlparse(asset_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or not parsed_url.path.startswith(
            f"/nanomatters/proton-cachyos/releases/download/{tag}/"
        )
    ):
        raise RuntimeError("GitHub returned an invalid Proton Wineland download URL.")
    digest_text = asset.get("digest", "")
    digest_match = re.fullmatch(r"sha256:([0-9a-f]{64})", digest_text)
    if digest_match is None:
        raise RuntimeError("GitHub did not provide a valid SHA-256 digest for the archive.")
    expected_digest = digest_match.group(1)
    expected_size = int(asset.get("size", 0))
    if expected_size <= 0:
        raise RuntimeError("GitHub returned an invalid Proton Wineland archive size.")

    staged = compatibility_dir / f".{directory_name}.new-{os.getpid()}"
    _remove_managed_path(staged)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".proton-wineland-download-",
            dir=compatibility_dir,
        ) as temporary_directory:
            temporary = Path(temporary_directory)
            archive_path = temporary / asset_name
            report(f"Downloading {asset_name} (0%)")
            digest = hashlib.sha256()
            downloaded = 0
            last_percent = -1
            download_request = urllib.request.Request(
                asset_url,
                headers={"User-Agent": "Proton Selector"},
            )
            with urllib.request.urlopen(download_request, timeout=60) as response:
                with archive_path.open("wb") as archive_file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > expected_size:
                            raise RuntimeError("The downloaded Wineland archive is larger than expected.")
                        archive_file.write(chunk)
                        digest.update(chunk)
                        percent = downloaded * 100 // expected_size
                        if percent != last_percent:
                            report(f"Downloading {asset_name} ({percent}%)")
                            last_percent = percent
            if downloaded != expected_size:
                raise RuntimeError("The downloaded Wineland archive is incomplete.")
            if digest.hexdigest() != expected_digest:
                raise RuntimeError("The Proton Wineland archive failed SHA-256 verification.")

            report("Extracting Proton Wineland...")
            extraction_root = temporary / "extracted"
            _safe_extract_tar(archive_path, extraction_root)
            tool_roots = [
                manifest.parent
                for manifest in extraction_root.rglob("compatibilitytool.vdf")
                if (manifest.parent / "proton").is_file()
                and (manifest.parent / "toolmanifest.vdf").is_file()
            ]
            if len(tool_roots) != 1:
                raise RuntimeError("The Wineland archive has an unexpected tool layout.")
            tool_root = tool_roots[0]
            launcher = tool_root / "proton"
            if not os.access(launcher, os.X_OK):
                raise RuntimeError("The downloaded Proton Wineland launcher is not executable.")
            os.replace(tool_root, staged)
            _atomic_write(
                staged / WINELAND_MANAGED_MARKER,
                json.dumps(
                    {
                        "variant": variant,
                        "tag": tag,
                        "asset": asset_name,
                        "sha256": expected_digest,
                    },
                    sort_keys=True,
                )
                + "\n",
            )
        report(f"Installing {display_name} {tag}...")
        _replace_managed_path(staged, destination)
    finally:
        _remove_managed_path(staged)

    return tag


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


def _read_proton_environment_file(path: Path) -> dict[str, str]:
    try:
        entries = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    variables = {}
    for entry in entries:
        if "=" in entry:
            name, value = entry.split("=", 1)
        else:
            name, value = entry, "1"
        if ENVIRONMENT_VARIABLE_NAME_PATTERN.fullmatch(name):
            variables[name] = value
    return variables


def _parse_proton_environment_text(text: str) -> dict[str, str]:
    variables = {}
    for line_number, entry in enumerate(text.splitlines(), start=1):
        if not entry.strip():
            continue
        if "=" in entry:
            name, value = entry.split("=", 1)
        else:
            name, value = entry, "1"
        if not ENVIRONMENT_VARIABLE_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                f"Invalid environment variable on line {line_number}. "
                "Use NAME=value."
            )
        variables[name] = value
    return variables


def _proton_environment_file_contents(
    variables: Mapping[str, str] | Iterable[str],
) -> str:
    entries = (
        variables.items()
        if isinstance(variables, Mapping)
        else ((name, "1") for name in variables)
    )
    configured = {}
    for name, value in entries:
        value = str(value)
        if (
            ENVIRONMENT_VARIABLE_NAME_PATTERN.fullmatch(name)
            and "\n" not in value
            and "\r" not in value
        ):
            configured[name] = value
    return "".join(f"{name}={configured[name]}\n" for name in sorted(configured))


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
        self.proton_environment_path = self.tool_path / "proton-environment"
        self.game_environment_dir = self.tool_path / "game-environment"
        self.legacy_adapter_path = data_home(self.home, self.env) / "tool"

    def proton_environment_variables(self) -> dict[str, str]:
        return _read_proton_environment_file(self.proton_environment_path)

    def proton_environment_options(self) -> set[str]:
        return set(self.proton_environment_variables())

    def save_proton_environment(
        self,
        variables: Mapping[str, str] | Iterable[str],
    ) -> None:
        _atomic_write(
            self.proton_environment_path,
            _proton_environment_file_contents(variables),
        )

    def game_proton_environment_variables(self, game_id: str) -> dict[str, str]:
        normalized = normalize_game_id(game_id)
        if not normalized:
            return {}
        return _read_proton_environment_file(
            self.game_environment_dir / f"{normalized}.env"
        )

    def all_game_proton_environment_variables(self) -> dict[str, dict[str, str]]:
        try:
            files = self.game_environment_dir.glob("*.env")
            return {
                game_id: _read_proton_environment_file(path)
                for path in files
                if (game_id := normalize_game_id(path.stem))
            }
        except OSError:
            return {}

    def save_game_proton_environment(
        self,
        game_id: str,
        variables: Mapping[str, str],
    ) -> None:
        normalized = normalize_game_id(game_id)
        if not normalized:
            return
        environment_path = self.game_environment_dir / f"{normalized}.env"
        contents = _proton_environment_file_contents(variables)
        if not contents:
            environment_path.unlink(missing_ok=True)
            return
        self.game_environment_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(environment_path, contents)

    def save_game_proton_environments(
        self,
        environments: Mapping[str, Mapping[str, str]],
    ) -> None:
        configured = {}
        for game_id, variables in environments.items():
            normalized = normalize_game_id(game_id)
            contents = _proton_environment_file_contents(variables)
            if normalized and contents:
                configured[normalized] = contents
        if self.game_environment_dir.is_dir():
            for path in self.game_environment_dir.glob("*.env"):
                if path.stem not in configured:
                    path.unlink(missing_ok=True)
        elif configured:
            self.game_environment_dir.mkdir(parents=True, exist_ok=True)
        for game_id, contents in configured.items():
            _atomic_write(self.game_environment_dir / f"{game_id}.env", contents)

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

    def clear_game_mapping(self, game_id: str) -> bool:
        normalized = normalize_game_id(game_id)
        if not normalized or not self._validate_existing_tool_path():
            return False
        mappings = self._read_game_mappings()
        mapping = mappings.pop(normalized, None)
        if mapping is None:
            return False
        self._write_game_mappings(self.tool_path, mappings)
        _remove_managed_path(self.games_dir / mapping.managed_directory)
        return True

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
        proton_environment: Mapping[str, str] | Iterable[str] | None = None,
        game_proton_environment: Mapping[str, Mapping[str, str]] | None = None,
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

        if proton_environment is not None:
            self.save_proton_environment(proton_environment)
        if game_proton_environment is not None:
            self.save_game_proton_environments(game_proton_environment)
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
    from proton_selector_qt import run_application

    return run_application(sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
