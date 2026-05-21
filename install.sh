#!/bin/bash
set -e

PACKAGE_DIR="/package"

# Install icon
install -Dm644 data/vajo.png $PACKAGE_DIR/usr/share/pixmaps/vajo.png

# Install desktop entry
install -Dm644 data/vajo.desktop $PACKAGE_DIR/usr/share/applications/vajo.desktop

# Install executables
install -Dm755 src/vajo_tui.py $PACKAGE_DIR/usr/bin/vajo_tui.py
install -Dm755 src/vajo_gui.py $PACKAGE_DIR/usr/bin/vajo_gui.py

# Create convenient symlinks for easier launching
ln -sf vajo_gui.py $PACKAGE_DIR/usr/bin/vajo-gui
ln -sf vajo_tui.py $PACKAGE_DIR/usr/bin/vajo-tui

# Install polkit policy + rules
install -Dm644 polkit/org.mocaccino.vajo.policy $PACKAGE_DIR/usr/share/polkit-1/actions/org.mocaccino.vajo.policy
install -Dm644 polkit/99-luet.rules $PACKAGE_DIR/etc/polkit-1/rules.d/99-luet.rules
chown root:root $PACKAGE_DIR/etc/polkit-1/rules.d/99-luet.rules

# Install translations
for lang in $(ls locale 2>/dev/null); do
    if [ -f "locale/$lang/LC_MESSAGES/vajo_ui.mo" ]; then
        install -Dm644 "locale/$lang/LC_MESSAGES/vajo_ui.mo" \
            "$PACKAGE_DIR/usr/share/locale/$lang/LC_MESSAGES/vajo_ui.mo"
    fi
done

# Install Python core module to a version-independent location
SHARED_DIR="$PACKAGE_DIR/usr/share/vajo"
install -d "$SHARED_DIR"
install -m644 src/vajo_core.py "$SHARED_DIR/vajo_core.py"

# Install submodules
install -d "$SHARED_DIR/modules"
install -m644 src/modules/__init__.py "$SHARED_DIR/modules/__init__.py"
install -m644 src/modules/i18n.py "$SHARED_DIR/modules/i18n.py"
install -m644 src/modules/rollback.py "$SHARED_DIR/modules/rollback.py"
