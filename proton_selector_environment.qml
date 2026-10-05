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
                        + " " + variable.inheritedValue
                    return searchableText.toLocaleLowerCase().includes(query)
                }
            )
        }

        function refreshEnvironmentVariablesEditor() {
            var value = environmentPage.selector.environmentVariablesText
            if (environmentVariablesEditor.text !== value) {
                environmentVariablesEditor.text = value
            }
        }

        title: "Proton Environment"

        footer: Controls.ToolBar {
            padding: Kirigami.Units.smallSpacing

            RowLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing

                Item {
                    Layout.fillWidth: true
                }

                Controls.Button {
                    text: "Cancel"
                    icon.name: "dialog-cancel"
                    enabled: (
                        environmentPage.selector.environmentChangesPending
                        || environmentPage.selector.environmentVariablesTextError.length > 0
                    ) && !environmentPage.selector.copying
                    onClicked: {
                        environmentPage.selector.cancelEnvironmentChanges()
                        environmentPage.refreshEnvironmentVariablesEditor()
                    }
                }

                Controls.Button {
                    text: "Apply"
                    icon.name: "dialog-ok-apply"
                    enabled: environmentPage.selector.environmentChangesPending
                        && environmentPage.selector.environmentVariablesTextError.length === 0
                        && !environmentPage.selector.copying
                    onClicked: environmentPage.selector.applyEnvironmentChanges()
                }
            }
        }

        ColumnLayout {
            width: environmentPage.width
            spacing: Kirigami.Units.largeSpacing

            RowLayout {
                Layout.fillWidth: true

                Controls.Label {
                    text: "Apply to"
                }

                Controls.ComboBox {
                    Layout.fillWidth: true
                    model: environmentPage.selector.environmentScopes
                    textRole: "label"
                    currentIndex: environmentPage.selector.environmentScopeIndex
                    onActivated: {
                        environmentPage.selector.setEnvironmentScopeIndex(
                            currentIndex
                        )
                        environmentPage.refreshEnvironmentVariablesEditor()
                    }
                    Accessible.name: "Environment variable scope"
                }
            }

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
                        Layout.preferredWidth:350
                        Layout.minimumWidth: 240
                        text: modelData.name
                        elide: Text.ElideRight
                        Accessible.name: modelData.name
                    }

                    Controls.ComboBox {
                        Layout.fillWidth: true
                        visible: modelData.type === "boolean"
                        model: environmentPage.selector.environmentBooleanOptions
                        textRole: "label"
                        currentIndex: modelData.booleanIndex
                        enabled: !environmentPage.selector.copying
                        onActivated: {
                            environmentPage.selector.setProtonEnvironmentVariable(
                                modelData.name,
                                environmentPage.selector.environmentBooleanOptions[
                                    currentIndex
                                ].value
                            )
                        }
                        Controls.ToolTip.text: modelData.inheritedValue.length > 0
                            ? "Global default: " + modelData.inheritedValue
                            : ""
                        Accessible.name: modelData.name + " boolean value"
                    }

                    Controls.TextField {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 150
                        visible: modelData.type === "string"
                        text: modelData.value
                        placeholderText: modelData.inheritedValue.length > 0
                            ? "Inherited: " + modelData.inheritedValue
                            : "Not set"
                        enabled: !environmentPage.selector.copying
                        onTextEdited: {
                            environmentPage.selector.setProtonEnvironmentVariable(
                                modelData.name,
                                text
                            )
                        }
                        Accessible.name: modelData.name + " value"
                    }
                }
            }

            Controls.Label {
                Layout.fillWidth: true
                text: "Environment variables (one per line)"
                wrapMode: Text.WordWrap
            }

            Controls.ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 140

                Controls.TextArea {
                    id: environmentVariablesEditor
                    width: parent.width
                    placeholderText: "NAME=value"
                    wrapMode: TextEdit.Wrap
                    enabled: !environmentPage.selector.copying
                    Accessible.name: "Environment variables"
                    Component.onCompleted: {
                        text = environmentPage.selector.environmentVariablesText
                    }
                    onTextChanged: environmentPage.selector.setEnvironmentVariablesText(
                        text
                    )
                }
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: environmentPage.selector.environmentVariablesTextError.length > 0
                text: environmentPage.selector.environmentVariablesTextError
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.WordWrap
            }
        }
    }
}
