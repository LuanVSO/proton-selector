import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: environmentWindow
    property var appController: protonSelector

    width: 580
    height: 520
    minimumWidth: 480
    minimumHeight: 320
    visible: false
    title: "Proton Environment"

    Shortcut {
        sequence: "Ctrl+F"
        onActivated: {
            variableSearchField.forceActiveFocus()
            variableSearchField.selectAll()
        }
    }

    pageStack.initialPage: Kirigami.ScrollablePage {
        id: environmentPage
        property var selector: environmentWindow.appController
        property var filteredVariables: {
            var query = variableSearchField.text.trim().toLocaleLowerCase()
            return environmentPage.selector.protonEnvironmentVariables.filter(
                function(variable) {
                    var searchableText = variable.name + " " + variable.value
                    return searchableText.toLocaleLowerCase().includes(query)
                }
            )
        }

        title: "Proton Environment"

        ColumnLayout {
            width: environmentPage.width
            spacing: Kirigami.Units.largeSpacing

            Controls.TextField {
                id: variableSearchField
                Layout.fillWidth: true
                placeholderText: "Search variables or values"
                Accessible.name: placeholderText
            }

            Controls.Label {
                Layout.fillWidth: true
                text: "Variables detected in " + environmentPage.selector.activeVersionName
                wrapMode: Text.WordWrap
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: environmentPage.selector.protonEnvironmentVariables.length === 0
                text: "No environment variables were detected in this Proton launcher."
                wrapMode: Text.WordWrap
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: environmentPage.selector.protonEnvironmentVariables.length > 0
                    && environmentPage.filteredVariables.length === 0
                text: "No variables match your search."
                wrapMode: Text.WordWrap
            }

            Repeater {
                model: environmentPage.filteredVariables

                delegate: RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.largeSpacing

                    Controls.Label {
                        Layout.preferredWidth: 240
                        Layout.minimumWidth: 140
                        text: modelData.name
                        elide: Text.ElideRight
                        Accessible.name: modelData.name
                    }

                    Controls.TextField {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 150
                        text: modelData.value
                        placeholderText: "Not set"
                        enabled: !environmentPage.selector.copying
                        onEditingFinished: environmentPage.selector.setProtonEnvironmentVariable(
                            modelData.name,
                            text
                        )
                        Accessible.name: modelData.name + " value"
                    }
                }
            }
        }
    }
}
