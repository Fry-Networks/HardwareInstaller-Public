"""Mysterium TOS consent dialog for the installer wizard.

Lifted from HardwareExe-git miner_GUI/ui/helpers/integrations/mysterium.py
(show_mysterium_consent_dialog, lines 358-403) and adapted for standalone use
in the installer.  The installer does not have access to MainWindow, so this
version accepts a plain QWidget parent and a screen-size preference string.
"""

from PySide6 import QtCore, QtWidgets


def show_mysterium_consent_dialog(
    parent: QtWidgets.QWidget,
    screen_size_pref: str = "auto",
) -> bool:
    """Show MystNodes SDK consent disclaimer and return ``True`` if user agrees."""
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("MystNodes SDK Sharing Consent")
    dialog.setModal(True)

    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(16)

    disclaimer = QtWidgets.QLabel(
        "By enabling MystNodes SDK, you agree to share your unused internet bandwidth "
        "with the MystNodes network. This allows others to route their internet traffic "
        "through your connection to earn bandwidth rewards.<br><br>"
        "Your participation is voluntary and you can opt-out at any time by toggling "
        "this setting off.<br><br>"
        "Please review the "
        "<a href='https://mysterium.network/terms-conditions/'>Mysterium Terms &amp; Conditions</a> "
        "and <a href='https://mysterium.network/privacy-policy/'>Privacy Policy</a> "
        "for more information."
    )
    disclaimer.setWordWrap(True)
    disclaimer.setTextFormat(QtCore.Qt.TextFormat.RichText)
    disclaimer.setOpenExternalLinks(True)
    disclaimer.setStyleSheet("a { color: #4ea3ff; }")
    layout.addWidget(disclaimer)

    button_layout = QtWidgets.QHBoxLayout()
    button_layout.addStretch(1)

    decline_btn = QtWidgets.QPushButton("Decline")
    decline_btn.clicked.connect(dialog.reject)
    button_layout.addWidget(decline_btn)

    agree_btn = QtWidgets.QPushButton("I Agree")
    agree_btn.setDefault(True)
    agree_btn.clicked.connect(dialog.accept)
    button_layout.addWidget(agree_btn)

    layout.addLayout(button_layout)

    if screen_size_pref == "mobile":
        dialog.setMinimumWidth(320)
        dialog.resize(420, dialog.sizeHint().height())
    else:
        dialog.setMinimumWidth(400)

    return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
