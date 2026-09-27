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

    pageStack.initialPage: Kirigami.ScrollablePage {
        id: gamePage
        property var selector: gameWindow.appController
        property string searchQuery: gameSearchField.text.trim().toLocaleLowerCase()
        property var filteredGames: {
            return gamePage.selector.installedGames.filter(function(game) {
                var searchableText = game.name + " " + game.gameId
                return searchableText.toLocaleLowerCase().includes(gamePage.searchQuery)
            })
        }

        title: "Installed Steam Games"

        ColumnLayout {
            width: parent.width
            spacing: Kirigami.Units.largeSpacing

            Controls.TextField {
                id: gameSearchField
                Layout.fillWidth: true
                placeholderText: "Search games by name or ID"
                Accessible.name: placeholderText
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: gamePage.selector.installedGames.length === 0
                text: "No installed Steam games found."
                wrapMode: Text.WordWrap
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: gamePage.selector.installedGames.length > 0
                    && gamePage.filteredGames.length === 0
                text: "No games match your search."
                wrapMode: Text.WordWrap
            }

            Kirigami.FormLayout {
                Layout.fillWidth: true

                Repeater {
                    model: gamePage.selector.installedGames

                    delegate: Controls.ComboBox {
                        Kirigami.FormData.label: modelData.name
                        Layout.fillWidth: true
                        visible: (modelData.name + " " + modelData.gameId)
                            .toLocaleLowerCase().includes(gamePage.searchQuery)
                        model: gamePage.selector.gameVersionOptions
                        textRole: "label"
                        currentIndex: modelData.versionIndex
                        enabled: !gamePage.selector.copying
                        onActivated: gamePage.selector.setInstalledGameVersion(
                            modelData.gameId,
                            currentIndex
                        )
                        Accessible.name: modelData.name + " Proton version"
                        Controls.ToolTip.text:
                            modelData.name + " (" + modelData.gameId + ")"
                    }
                }
            }
        }
    }
}
