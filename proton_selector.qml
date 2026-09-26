import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: mainWindow
    property var appController: protonSelector

    width: 720
    height: 760
    minimumWidth: 520
    minimumHeight: 560
    title: "Proton Selector"

    pageStack.initialPage: Kirigami.ScrollablePage {
        id: page
        property var selector: mainWindow.appController

        title: "Proton Selector"

        ColumnLayout {
            width: page.width
            spacing: Kirigami.Units.largeSpacing

            RowLayout {
                Layout.fillWidth: true

                Item {
                    Layout.fillWidth: true
                }

                Controls.Label {
                    text: page.selector.translations.language
                }

                Controls.ComboBox {
                    model: page.selector.languageOptions
                    currentIndex: page.selector.languageIndex
                    onActivated: page.selector.setLanguageIndex(currentIndex)
                    Accessible.name: page.selector.translations.language
                }
            }

            Kirigami.FormLayout {
                Layout.fillWidth: true

                Controls.ComboBox {
                    Kirigami.FormData.label: page.selector.translations.active_version
                    Layout.fillWidth: true
                    model: page.selector.versionOptions
                    textRole: "label"
                    currentIndex: page.selector.activeIndex
                    enabled: !page.selector.copying
                    onActivated: page.selector.setActiveIndex(currentIndex)
                    Accessible.name: page.selector.translations.active_version
                }

                Controls.ComboBox {
                    Kirigami.FormData.label: page.selector.translations.fallback_version
                    Layout.fillWidth: true
                    model: page.selector.versionOptions
                    textRole: "label"
                    currentIndex: page.selector.fallbackIndex
                    enabled: !page.selector.copying
                    onActivated: page.selector.setFallbackIndex(currentIndex)
                    Accessible.name: page.selector.translations.fallback_version
                }

            }

            Controls.Button {
                Layout.alignment: Qt.AlignLeft
                text: "Game Versions..."
                icon.name: "applications-games"
                enabled: !page.selector.copying
                onClicked: page.selector.openGameWindow()
            }

            Controls.Label {
                Layout.fillWidth: true
                text: "Common Proton Environment Options"
                font.bold: true
            }

            Repeater {
                model: page.selector.protonEnvironmentOptions

                delegate: RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.largeSpacing

                    Controls.Switch {
                        checked: modelData.enabled
                        enabled: !page.selector.copying
                        text: modelData.name
                        onToggled: page.selector.setProtonEnvironmentOption(
                            modelData.name,
                            checked
                        )
                        Accessible.name: modelData.name
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        text: modelData.description
                        wrapMode: Text.WordWrap
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: Kirigami.Units.largeSpacing
                rowSpacing: Kirigami.Units.smallSpacing

                Controls.Label { text: page.selector.translations.runtime_appid }
                Controls.Label {
                    Layout.fillWidth: true
                    text: page.selector.runtimeAppId
                    elide: Text.ElideRight
                    Accessible.selectableText: true
                }

                Controls.Label { text: page.selector.translations.location }
                Controls.Label {
                    Layout.fillWidth: true
                    text: page.selector.location
                    elide: Text.ElideMiddle
                    Accessible.selectableText: true
                    Controls.ToolTip.text: page.selector.location
                }

                Controls.Label { text: page.selector.translations.source }
                Controls.Label {
                    Layout.fillWidth: true
                    text: page.selector.source
                    elide: Text.ElideRight
                    Accessible.selectableText: true
                    Controls.ToolTip.text: page.selector.source
                }
            }

            Controls.Label {
                Layout.fillWidth: true
                text: page.selector.statusText
                wrapMode: Text.WordWrap
                Accessible.selectableText: true
            }

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: page.selector.notificationVisible
                type: page.selector.notificationIsError
                    ? Kirigami.MessageType.Error
                    : Kirigami.MessageType.Information
                text: page.selector.notificationTitle + "\n" + page.selector.notificationBody
                showCloseButton: true
                onVisibleChanged: {
                    if (!visible) {
                        page.selector.closeNotification()
                    }
                }
            }

            Controls.ProgressBar {
                Layout.fillWidth: true
                visible: page.selector.copying
                indeterminate: true
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: page.selector.copying
                text: page.selector.progressText
                wrapMode: Text.WordWrap
            }

            Item {
                Layout.fillHeight: true
                Layout.minimumHeight: Kirigami.Units.smallSpacing
            }

            Kirigami.Separator {
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true

                Controls.Button {
                    text: page.selector.translations.refresh
                    icon.name: "view-refresh"
                    enabled: !page.selector.copying
                    onClicked: page.selector.refresh()
                }

                Item { Layout.fillWidth: true }

                Controls.Button {
                    text: page.selector.copying
                        ? page.selector.translations.copying
                        : page.selector.translations.use_selected
                    icon.name: "dialog-ok-apply"
                    enabled: page.selector.canActivate
                    highlighted: true
                    onClicked: page.selector.activateSelected()
                }
            }
        }
    }
}
