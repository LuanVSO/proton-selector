"""Qt Quick and Kirigami interface for Proton Selector."""

from __future__ import annotations

import os
import threading
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle


class SelectorController(QObject):
    stateChanged = Signal()
    notificationChanged = Signal()
    progressChanged = Signal()
    _copyProgress = Signal(str)
    _copyFinished = Signal(object, object, object, object, object)
    _winelandUpdateFinished = Signal(object, object, object)

    def __init__(self, backend: ModuleType) -> None:
        super().__init__()
        self.backend = backend
        self.manager = backend.SelectorManager()
        self._game_window: Any | None = None
        self._environment_window: Any | None = None
        self.tools: list[Any] = []
        self._installed_games: list[Any] = []
        self.base_tool: Any | None = None
        self._active_index = -1
        self._fallback_index = -1
        self._game_index = -1
        self._game_id = ""
        self._game_id_error = ""
        self._copying = False
        self._wineland_updating = False
        self._wineland_variant = "normal"
        self._wineland_variant_user_selected = False
        self._pending_game_versions: dict[str, int] = {}
        self._progress_text = ""
        self._notification_title = ""
        self._notification_body = ""
        self._notification_is_error = False
        self._notification_visible = False
        self._proton_environment_values = self.manager.proton_environment_variables()
        self._available_proton_environment_variables: tuple[str, ...] = ()
        language_override = os.environ.get("PROTON_SELECTOR_LANGUAGE", "")
        if language_override:
            self.language_preference = backend._.language
        else:
            self.language_preference = backend.saved_language_preference()
            if self.language_preference == "system":
                backend._.use_system_language()
            else:
                backend._.set_language(self.language_preference)
        self._copyProgress.connect(self._set_copy_progress)
        self._copyFinished.connect(self._activation_finished)
        self._winelandUpdateFinished.connect(self._wineland_update_finished)
        self.refresh()

    @Property("QVariantList", notify=stateChanged)
    def versionOptions(self) -> list[dict[str, str]]:
        name_counts = Counter(tool.display_name for tool in self.tools)
        options = []
        for tool in self.tools:
            label = tool.display_name
            if name_counts[tool.display_name] > 1:
                label = f"{label} — {tool.path}"
            options.append({"label": label})
        return options

    @Property("QVariantList", notify=stateChanged)
    def winelandVariants(self) -> list[dict[str, str]]:
        return [
            {"value": "normal", "label": "Normal"},
            {"value": "v3", "label": "_v3"},
            {"value": "wow64", "label": "_wow64"},
        ]

    @Property(int, notify=stateChanged)
    def winelandVariantIndex(self) -> int:
        return {"normal": 0, "v3": 1, "wow64": 2}[self._wineland_variant]

    @Property(bool, notify=stateChanged)
    def winelandUpdating(self) -> bool:
        return self._wineland_updating

    @Property("QVariantList", notify=stateChanged)
    def gameVersionOptions(self) -> list[dict[str, str]]:
        return [
            {"label": "Use Active Version"},
            *self.versionOptions,
        ]

    @Property("QVariantList", notify=stateChanged)
    def installedGames(self) -> list[dict[str, Any]]:
        mappings = self.manager.game_mappings()
        games = []
        for game in self._installed_games:
            mapping = mappings.get(game.game_id)
            version_index = 0
            if mapping:
                mapped_index = self._tool_index_for_path(mapping.source_path)
                if mapped_index >= 0:
                    version_index = mapped_index + 1
            version_index = self._pending_game_versions.get(
                game.game_id,
                version_index,
            )
            games.append(
                {
                    "gameId": game.game_id,
                    "name": game.name,
                    "versionIndex": version_index,
                }
            )
        return games

    @Property("QVariantMap", notify=stateChanged)
    def translations(self) -> dict[str, str]:
        return dict(self.backend._.catalog)

    @Property("QVariantList", notify=stateChanged)
    def protonEnvironmentVariables(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "value": self._proton_environment_values.get(name, ""),
            }
            for name in self._available_proton_environment_variables
        ]

    @Property(str, notify=stateChanged)
    def activeVersionName(self) -> str:
        tool = self._tool_at(self._active_index)
        return tool.display_name if tool else "—"

    @Property("QVariantList", notify=stateChanged)
    def languageOptions(self) -> list[str]:
        return [
            self.backend._("system_default"),
            *self.backend.LANGUAGE_NAMES.values(),
        ]

    @Property(int, notify=stateChanged)
    def languageIndex(self) -> int:
        codes = ["system", *self.backend.LANGUAGE_NAMES]
        try:
            return codes.index(self.language_preference)
        except ValueError:
            return 0

    @Property(int, notify=stateChanged)
    def activeIndex(self) -> int:
        return self._active_index

    @Property(int, notify=stateChanged)
    def fallbackIndex(self) -> int:
        return self._fallback_index

    @Property(int, notify=stateChanged)
    def gameIndex(self) -> int:
        return self._game_index

    @Property(str, notify=stateChanged)
    def gameId(self) -> str:
        return self._game_id

    @Property(str, notify=stateChanged)
    def gameIdError(self) -> str:
        return self._game_id_error

    @Property(bool, notify=stateChanged)
    def gameIdValid(self) -> bool:
        return not self._game_id_error

    @Property(bool, notify=stateChanged)
    def gameVersionEnabled(self) -> bool:
        return bool(self._game_id) and self.gameIdValid and not self.copying

    @Property(bool, notify=stateChanged)
    def canActivate(self) -> bool:
        return (
            not self.copying
            and self._tool_at(self._active_index) is not None
            and self._tool_at(self._fallback_index) is not None
            and self.base_tool is not None
            and self.gameIdValid
            and (not self._game_id or self._tool_at(self._game_index) is not None)
        )

    @Property(str, notify=stateChanged)
    def runtimeAppId(self) -> str:
        tool = self._tool_at(self._active_index)
        return (tool.runtime_appid or "—") if tool else "—"

    @Property(str, notify=stateChanged)
    def location(self) -> str:
        tool = self._tool_at(self._active_index)
        return str(tool.path) if tool else "—"

    @Property(str, notify=stateChanged)
    def source(self) -> str:
        tool = self._tool_at(self._active_index)
        return tool.source if tool else "—"

    @Property(str, notify=stateChanged)
    def statusText(self) -> str:
        active, fallback = self._status_versions()
        return (
            f'{self.backend._("status_active", version=active)}\n'
            f'{self.backend._("status_fallback", version=fallback)}'
        )

    @Property(bool, notify=stateChanged)
    def copying(self) -> bool:
        return self._copying or self._wineland_updating

    @Property(str, notify=progressChanged)
    def progressText(self) -> str:
        return self._progress_text

    @Property(bool, notify=notificationChanged)
    def notificationVisible(self) -> bool:
        return self._notification_visible

    @Property(str, notify=notificationChanged)
    def notificationTitle(self) -> str:
        return self._notification_title

    @Property(str, notify=notificationChanged)
    def notificationBody(self) -> str:
        return self._notification_body

    @Property(bool, notify=notificationChanged)
    def notificationIsError(self) -> bool:
        return self._notification_is_error

    @Slot(int)
    def setLanguageIndex(self, index: int) -> None:
        codes = ["system", *self.backend.LANGUAGE_NAMES]
        if index < 0 or index >= len(codes):
            return
        preference = codes[index]
        if preference == self.language_preference:
            return
        self.language_preference = preference
        if preference == "system":
            self.backend._.use_system_language()
        else:
            self.backend._.set_language(preference)
        self.backend.save_language_preference(preference)
        self.stateChanged.emit()

    @Slot(int)
    def setActiveIndex(self, index: int) -> None:
        if self.copying:
            return
        self._active_index = index if self._tool_at(index) else -1
        self._update_proton_environment_variables()
        self.stateChanged.emit()

    @Slot(int)
    def setFallbackIndex(self, index: int) -> None:
        if self.copying:
            return
        self._fallback_index = index if self._tool_at(index) else -1
        self.stateChanged.emit()

    @Slot(int)
    def setGameIndex(self, index: int) -> None:
        self._game_index = index if self._tool_at(index) else -1
        self.stateChanged.emit()

    @Slot(str, int)
    def setInstalledGameVersion(self, game_id: str, version_index: int) -> None:
        if self.copying:
            return
        try:
            normalized_game_id = self.backend.normalize_game_id(game_id)
        except RuntimeError as error:
            self._notify(str(error), error=True)
            return
        if version_index == 0:
            if self.manager.clear_game_mapping(normalized_game_id):
                self._notify(
                    f"Game {normalized_game_id} now uses the active Proton version."
                )
                self.stateChanged.emit()
            return

        game_tool = self._tool_at(version_index - 1)
        selected_tool = self._tool_at(self._active_index)
        fallback_tool = self._tool_at(self._fallback_index)
        if (
            game_tool is None
            or selected_tool is None
            or fallback_tool is None
            or self.base_tool is None
        ):
            return
        self._pending_game_versions[normalized_game_id] = version_index
        self._begin_activation(
            selected_tool,
            fallback_tool,
            normalized_game_id,
            game_tool,
        )

    @Slot(str)
    def setGameId(self, value: str) -> None:
        self._game_id = value
        self._game_id_error = ""
        try:
            game_id = self.backend.normalize_game_id(value)
        except RuntimeError as error:
            self._game_id_error = str(error)
        else:
            if game_id:
                mapping = self.manager.game_mapping(game_id)
                mapped_index = self._tool_index_for_path(
                    mapping.source_path if mapping else ""
                )
                self._game_index = (
                    mapped_index if mapped_index >= 0 else self._active_index
                )
        self.stateChanged.emit()

    @Slot(str, bool)
    def setProtonEnvironmentVariable(self, name: str, value: str) -> None:
        if self.copying:
            return
        if name not in self._available_proton_environment_variables:
            return
        if value:
            self._proton_environment_values[name] = value
        else:
            self._proton_environment_values.pop(name, None)
        if self.manager.tool_path.is_dir() and not self.manager.tool_path.is_symlink():
            self.manager.save_proton_environment(self._proton_environment_values)
        self.stateChanged.emit()

    @Slot()
    def refresh(self) -> None:
        if self.copying:
            return
        previous_paths = [
            self._path_at(self._active_index),
            self._path_at(self._fallback_index),
            self._path_at(self._game_index),
        ]
        current = self.manager.current_target()
        current_fallback = self.manager.current_fallback_target()
        current_path = str(current) if current else ""
        fallback_path = str(current_fallback) if current_fallback else ""

        self.tools = self.backend.scan_proton_tools()
        self._installed_games = self.backend.scan_installed_games(
            self.manager.home,
            self.manager.env,
        )
        self.base_tool = self.manager.find_base(self.tools)
        self._active_index = self._preferred_index(
            previous_paths[0] or current_path
        )
        if self._active_index < 0:
            self._active_index = self._base_index()
        if self._active_index < 0 and self.tools:
            self._active_index = 0

        self._fallback_index = self._preferred_index(
            previous_paths[1] or fallback_path
        )
        if self._fallback_index < 0:
            self._fallback_index = self._base_index()
        if self._fallback_index < 0 and self.tools:
            self._fallback_index = 0

        game_id = ""
        try:
            game_id = self.backend.normalize_game_id(self._game_id)
        except RuntimeError:
            pass
        game_path = previous_paths[2]
        if game_id and not game_path:
            mapping = self.manager.game_mapping(game_id)
            game_path = mapping.source_path if mapping else ""
        self._game_index = self._preferred_index(game_path)
        if self._game_index < 0:
            self._game_index = self._active_index
        self._sync_wineland_variant()
        self._update_proton_environment_variables()
        self.stateChanged.emit()

    @Slot()
    def activateSelected(self) -> None:
        selected_tool = self._tool_at(self._active_index)
        fallback_tool = self._tool_at(self._fallback_index)
        try:
            game_id = self.backend.normalize_game_id(self._game_id)
        except RuntimeError as error:
            self._notify(str(error), error=True)
            return
        game_tool = self._tool_at(self._game_index) if game_id else None
        if (
            not self.canActivate
            or selected_tool is None
            or fallback_tool is None
            or not self.base_tool
        ):
            return

        self._begin_activation(
            selected_tool,
            fallback_tool,
            game_id,
            game_tool,
        )

    def _begin_activation(
        self,
        selected_tool: Any,
        fallback_tool: Any,
        game_id: str,
        game_tool: Any | None,
    ) -> None:
        if self.copying or self.base_tool is None:
            return
        self._copying = True
        self._notification_visible = False
        self._progress_text = self.backend._("preparing")
        self.stateChanged.emit()
        self.progressChanged.emit()
        threading.Thread(
            target=self._activation_worker,
            args=(
                selected_tool,
                fallback_tool,
                self.base_tool,
                game_id,
                game_tool,
                dict(self._proton_environment_values),
            ),
            daemon=True,
        ).start()

    @Slot()
    def closeNotification(self) -> None:
        self._notification_visible = False
        self.notificationChanged.emit()

    def setGameWindow(self, game_window: Any) -> None:
        self._game_window = game_window

    @Slot()
    def openGameWindow(self) -> None:
        if self._game_window is None:
            return
        self._game_window.show()
        self._game_window.requestActivate()

    def setEnvironmentWindow(self, environment_window: Any) -> None:
        self._environment_window = environment_window

    @Slot()
    def openEnvironmentWindow(self) -> None:
        if self._environment_window is None:
            return
        self._environment_window.show()
        self._environment_window.requestActivate()

    @Slot(int)
    def setWinelandVariantIndex(self, index: int) -> None:
        variants = ("normal", "v3", "wow64")
        if self.copying or index < 0 or index >= len(variants):
            return
        self._wineland_variant = variants[index]
        self._wineland_variant_user_selected = True
        self.stateChanged.emit()

    @Slot()
    def updateWineland(self) -> None:
        if self.copying:
            return
        self._wineland_updating = True
        self._notification_visible = False
        self._progress_text = "Checking the latest Proton Wineland release..."
        self.stateChanged.emit()
        self.progressChanged.emit()
        threading.Thread(
            target=self._wineland_update_worker,
            args=(self._wineland_variant,),
            daemon=True,
        ).start()

    def _wineland_update_worker(self, variant: str) -> None:
        tag = None
        error = None
        try:
            tag = self.backend.install_proton_wineland(
                self.manager.compatibility_dir,
                variant,
                progress=self._copyProgress.emit,
            )
        except Exception as caught:
            error = caught
        self._winelandUpdateFinished.emit(variant, tag, error)

    @Slot(object, object, object)
    def _wineland_update_finished(
        self,
        variant: str,
        tag: str | None,
        error: Exception | None,
    ) -> None:
        self._wineland_updating = False
        self._progress_text = ""
        self.refresh()
        self.progressChanged.emit()
        if error is not None:
            self._notify(str(error), error=True)
            return
        if tag is None:
            self._notify(f"Proton Wineland {variant} is already up to date.")
            return
        self._notify(f"Proton Wineland {variant} was updated to {tag}.")

    @Slot(str, "QVariant", result=str)
    def translate(self, message_id: str, values: dict[str, Any] | None = None) -> str:
        return self.backend._(message_id, **dict(values or {}))

    def _tool_at(self, index: int) -> Any | None:
        return self.tools[index] if 0 <= index < len(self.tools) else None

    def _update_proton_environment_variables(self) -> None:
        tool = self._tool_at(self._active_index)
        self._available_proton_environment_variables = (
            self.backend.proton_environment_variables(tool.path)
            if tool
            else ()
        )

    def _sync_wineland_variant(self) -> None:
        if self._wineland_variant_user_selected:
            return
        installed = self.backend.installed_proton_wineland_variants(
            self.manager.compatibility_dir
        )
        active_tool = self._tool_at(self._active_index)
        active_variant = (
            self.backend.proton_wineland_variant(active_tool)
            if active_tool
            else None
        )
        if active_variant in installed:
            self._wineland_variant = active_variant
            return
        self._wineland_variant = next(
            (
                variant
                for variant in ("normal", "v3", "wow64")
                if variant in installed
            ),
            "normal",
        )

    def _path_at(self, index: int) -> str:
        tool = self._tool_at(index)
        return str(tool.canonical_path) if tool else ""

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

    def _preferred_index(self, path: str) -> int:
        return self._tool_index_for_path(path)

    def _base_index(self) -> int:
        if not self.base_tool:
            return -1
        return self._tool_index_for_path(str(self.base_tool.canonical_path))

    def _status_versions(self) -> tuple[str, str]:
        game_mapping = self.manager.game_mapping(self._game_id) if self._game_id and self.gameIdValid else None
        if game_mapping and self.manager.game_copy_available(game_mapping):
            active = game_mapping.proton_name
        elif game_mapping:
            active = self.backend._("files_missing", name=game_mapping.proton_name)
        else:
            saved_name = self.manager.saved_selection_name()
            if saved_name and self.manager.selected_copy_available():
                active = saved_name
            elif saved_name:
                active = self.backend._("files_missing", name=saved_name)
            else:
                active = self.backend._("none")

        fallback_name = self.manager.saved_fallback_name()
        if fallback_name and self.manager.fallback_copy_available():
            fallback = fallback_name
        elif fallback_name:
            fallback = self.backend._("files_missing", name=fallback_name)
        elif self.base_tool:
            fallback = self.base_tool.display_name
        else:
            fallback = self.backend._(
                "not_installed",
                name=self.manager.base_name,
            )
        return active, fallback

    def _notify(self, body: str, error: bool = False) -> None:
        self._notification_title = (
            self.backend._("error_title", app_name="Proton Selector")
            if error
            else "Proton Selector"
        )
        self._notification_body = body
        self._notification_is_error = error
        self._notification_visible = True
        self.notificationChanged.emit()

    def _set_copy_progress(self, message: str) -> None:
        self._progress_text = message
        self.progressChanged.emit()

    def _activation_worker(
        self,
        selected_tool: Any,
        fallback_tool: Any,
        base_tool: Any,
        game_id: str,
        game_tool: Any | None,
        proton_environment: dict[str, str],
    ) -> None:
        result = None
        error = None
        try:
            result = self.manager.activate(
                selected_tool,
                fallback_tool,
                base_tool,
                game_id=game_id,
                game_tool=game_tool,
                progress=self._copyProgress.emit,
                proton_environment=proton_environment,
            )
        except Exception as caught:
            error = caught
        self._copyFinished.emit(
            selected_tool,
            game_id,
            game_tool,
            result,
            error,
        )

    @Slot(object, object, object, object, object)
    def _activation_finished(
        self,
        selected_tool: Any,
        game_id: str,
        game_tool: Any,
        result: Any,
        error: Exception | None,
    ) -> None:
        self._copying = False
        self._progress_text = ""
        if game_id:
            self._pending_game_versions.pop(game_id, None)
        self.refresh()
        self.progressChanged.emit()
        if error is not None:
            self._notify(str(error), error=True)
            return
        if result is None:
            self._notify(self.backend._("operation_incomplete"), error=True)
            return
        if game_id and game_tool:
            success_text = (
                self.backend._(
                    "game_success",
                    game_id=game_id,
                    name=game_tool.display_name,
                )
                + "\n\n"
                + self.backend._("game_copies_ready")
            )
        else:
            success_text = (
                self.backend._("selector_success", name=selected_tool.display_name)
                + "\n\n"
                + self.backend._("default_copies_ready")
            )
        if result.first_activation:
            success_text += "\n\n" + self.backend._("restart_first")
        self._notify(success_text)


def run_application(backend: ModuleType) -> int:
    QQuickStyle.setStyle("org.kde.desktop")
    application = QGuiApplication.instance() or QGuiApplication([])
    application.setApplicationName("Proton Selector")
    application.setOrganizationDomain("io.github.protonselector")
    engine = QQmlApplicationEngine()
    controller = SelectorController(backend)
    engine.rootContext().setContextProperty("protonSelector", controller)
    qml_file = Path(__file__).with_name("proton_selector.qml")
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    if not engine.rootObjects():
        return 1
    game_qml_file = Path(__file__).with_name("proton_selector_games.qml")
    engine.load(QUrl.fromLocalFile(str(game_qml_file)))
    root_objects = engine.rootObjects()
    if len(root_objects) < 2:
        return 1
    controller.setGameWindow(root_objects[-1])
    environment_qml_file = Path(__file__).with_name(
        "proton_selector_environment.qml"
    )
    engine.load(QUrl.fromLocalFile(str(environment_qml_file)))
    root_objects = engine.rootObjects()
    if len(root_objects) < 3:
        return 1
    controller.setEnvironmentWindow(root_objects[-1])
    exit_code = application.exec()
    del engine
    del controller
    return exit_code
