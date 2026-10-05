import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: gameWindow
    property var appController: protonSelector

    width: 640
    height: 680
    minimumWidth: 500
    minimumHeight: 360
    visible: false
    title: "Proton Selector - Game Versions"

    Shortcut {
        sequence: "Ctrl+F"
        onActivated: {
            gameSearchField.forceActiveFocus()
            gameSearchField.selectAll()
        }
    }

    Shortcut {
        sequence: "Home"
        onActivated: gamePage.scrollToBoundary(false)
    }

    Shortcut {
        sequence: "End"
        onActivated: gamePage.scrollToBoundary(true)
    }

    Shortcut {
        sequence: "PgUp"
        onActivated: gamePage.scrollByPage(-1)
    }

    Shortcut {
        sequence: "PgDown"
        onActivated: gamePage.scrollByPage(1)
    }

    pageStack.initialPage: Kirigami.ScrollablePage {
        id: gamePage
        property var selector: gameWindow.appController
        property string searchQuery: gameSearchField.text.trim().toLocaleLowerCase()

        function scrollToBoundary(toEnd) {
            var minimumY = flickable.originY
            var maximumY = Math.max(
                minimumY,
                minimumY + flickable.contentHeight - flickable.height
            )
            flickable.contentY = toEnd ? maximumY : minimumY
        }

        function scrollByPage(direction) {
            var minimumY = flickable.originY
            var maximumY = Math.max(
                minimumY,
                minimumY + flickable.contentHeight - flickable.height
            )
            flickable.contentY = Math.max(
                minimumY,
                Math.min(
                    maximumY,
                    flickable.contentY + direction * flickable.height
                )
            )
        }

        title: "Installed Steam Games"

        header: Controls.ToolBar {
            padding: Kirigami.Units.largeSpacing

            RowLayout {
                anchors.fill: parent

                Controls.TextField {
                    id: gameSearchField
                    Layout.fillWidth: true
                    placeholderText: "Search games by name or ID"
                    Accessible.name: placeholderText
                }
            }
        }

        footer: Controls.ToolBar {
            padding: Kirigami.Units.smallSpacing

            RowLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing

                Item {
                    Layout.fillWidth: true
                }

                Controls.Button {
                    text: "Apply"
                    icon.name: "dialog-ok-apply"
                    enabled: gamePage.selector.gameChangesPending
                        && !gamePage.selector.copying
                    onClicked: gamePage.selector.applyGameChanges()
                }
            }
        }

        ColumnLayout {
            id: gamesColumn
            width: parent.width
            spacing: Kirigami.Units.largeSpacing

            // Capture these here so delegates never touch gamePage
            readonly property var selector: gameWindow.appController
            readonly property string searchQuery: gameSearchField.text.trim().toLocaleLowerCase()

            Controls.Label {
                Layout.fillWidth: true
                visible: !gamesColumn.selector
                    || gamesColumn.selector.installedGames.length === 0
                text: "No installed Steam games found."
                wrapMode: Text.WordWrap
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: gamesColumn.selector
                    && gamesColumn.selector.installedGames.length > 0
                    && !gamesRepeater.hasVisibleGame
                text: "No games match your search."
                wrapMode: Text.WordWrap
            }

            Repeater {
                id: gamesRepeater
                model: gamesColumn.selector
                    ? gamesColumn.selector.installedGames
                    : []

                property bool hasVisibleGame: false

                function refreshVisibleCount() {
                    var query = gamesColumn.searchQuery
                    var games = gamesColumn.selector
                        ? gamesColumn.selector.installedGames
                        : []
                    var any = false
                    for (var i = 0; i < games.length; ++i) {
                        var text = (games[i].name + " " + games[i].gameId)
                            .toLocaleLowerCase()
                        if (text.includes(query)) {
                            any = true
                            break
                        }
                    }
                    hasVisibleGame = any
                }

                onModelChanged: refreshVisibleCount()
                Component.onCompleted: refreshVisibleCount()

                Connections {
                    target: gameSearchField
                    function onTextChanged() {
                        gamesRepeater.refreshVisibleCount()
                    }
                }

                delegate: RowLayout {
                    id: gameRow
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.largeSpacing

                    // Injected by Repeater (do NOT mark these required)
                    readonly property var game: modelData
                    readonly property var selector: gamesColumn.selector
                    readonly property bool rowVisible: {
                        var text = (gameRow.game.name + " " + gameRow.game.gameId)
                            .toLocaleLowerCase()
                        return text.includes(gamesColumn.searchQuery)
                    }

                    visible: rowVisible
                    height: rowVisible ? implicitHeight : 0
                    enabled: rowVisible

                    Controls.Label {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                        elide: Text.ElideRight
                        wrapMode: Text.NoWrap
                        text: gameRow.game.name
                        Accessible.ignored: true
                    }

                    Controls.ComboBox {
                        id: versionBox

                        model: gameRow.selector
                            ? gameRow.selector.gameVersionOptions
                            : []
                        textRole: "label"
                        currentIndex: gameRow.game.versionIndex
                        enabled: gameRow.selector && !gameRow.selector.copying
                        Accessible.name: gameRow.game.name + " Proton version"
                        Controls.ToolTip.text:
                            gameRow.game.name + " (" + gameRow.game.gameId + ")"

                        onActivated: function(index) {
                            var sel = gameRow.selector
                            var gameId = gameRow.game ? gameRow.game.gameId : undefined
                            if (!sel || gameId === undefined)
                                return
                            sel.setInstalledGameVersion(gameId, index)
                        }
                    }
                }
            }
        }
    }
}
