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

    pageStack.initialPage: Kirigami.ScrollablePage {
        id: gamePage
        property var selector: gameWindow.appController

        title: "Installed Steam Games"

        ColumnLayout {
            width: parent.width
            spacing: Kirigami.Units.largeSpacing

            Controls.Label {
                Layout.fillWidth: true
                visible: gamePage.selector.installedGames.length === 0
                text: "No installed Steam games found."
                wrapMode: Text.WordWrap
            }

            Repeater {
                model: gamePage.selector.installedGames

                delegate: RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.largeSpacing

                    Controls.Label {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 80
                        text: modelData.name
                        elide: Text.ElideRight
                        Controls.ToolTip.text:
                            modelData.name + " (" + modelData.gameId + ")"
                    }

                    Controls.ComboBox {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 180
                        model: gamePage.selector.gameVersionOptions
                        textRole: "label"
                        currentIndex: modelData.versionIndex
                        enabled: !gamePage.selector.copying
                        onActivated: gamePage.selector.setInstalledGameVersion(
                            modelData.gameId,
                            currentIndex
                        )
                        Accessible.name: modelData.name + " Proton version"
                    }
                }
            }
        }
    }
}
