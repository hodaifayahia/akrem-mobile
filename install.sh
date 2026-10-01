#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/AkremMobileApp"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

echo "============================================================"
echo "  AkremMobile — Application Installer"
echo "============================================================"

# 1. Build if not already built
if [ ! -f "$PROJECT_DIR/dist/AkremMobile/AkremMobile" ]; then
    echo "[*] Building application binary first..."
    "$PROJECT_DIR/build.sh" --skip-zip
fi

# 2. Install app files
echo "[*] Installing app files to $APP_DIR..."
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "$APP_DIR"/*
cp -r "$PROJECT_DIR/dist/AkremMobile"/* "$APP_DIR/"

# 3. Create CLI launcher
echo "[*] Creating launcher command in $BIN_DIR/akremmobile..."
cat > "$BIN_DIR/akremmobile" << 'EOF'
#!/usr/bin/env bash
exec "$HOME/.local/share/AkremMobileApp/AkremMobile" "$@"
EOF
chmod +x "$BIN_DIR/akremmobile"

# 4. Copy application icon
if [ -f "$PROJECT_DIR/app/resources/logo.png" ]; then
    cp "$PROJECT_DIR/app/resources/logo.png" "$ICON_DIR/akremmobile.png"
fi

# 5. Create Desktop Entry (.desktop)
echo "[*] Creating Desktop application entry..."
cat > "$DESKTOP_DIR/akremmobile.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=AkremMobile
Comment=أكرم موبايل - إدارة التقسيط والمبيعات
Exec=$HOME/.local/bin/akremmobile
Icon=akremmobile
Terminal=false
Categories=Office;Finance;
StartupWMClass=AkremMobile
EOF
chmod +x "$DESKTOP_DIR/akremmobile.desktop"

echo "============================================================"
echo "  ✅ Installation Complete!"
echo "============================================================"
echo "  • Installed at:  $APP_DIR"
echo "  • Launch command: akremmobile"
echo "  • Desktop menu:  Added to Applications menu"
echo "============================================================"
echo ""
echo "To run the app right now, simply execute:"
echo "  ~/.local/bin/akremmobile"
echo ""
