#!/usr/bin/env bash
#
# Gaming Command Center — AppImage build, INSIDE an Ubuntu container.
# Runs as root inside ubuntu:22.04 (old glibc + classic gdk-pixbuf loaders, no
# glycin). Invoked by build-appimage-container.sh via podman. Reads the repo
# from /src (read-only) and writes the finished .AppImage to /out.
#
# Building on an old, stable base is what makes the AppImage both portable
# (glibc 2.35 runs nearly everywhere) and crash-free (no bleeding-edge glycin
# image loader to relocate).

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export APPIMAGE_EXTRACT_AND_RUN=1   # containers have no FUSE
export NO_STRIP=1                   # keep it simple/robust
export DEPLOY_GTK_VERSION=4         # the gtk plugin can't auto-detect from a py app

SRC=/src
OUT=/out
BUILD=/tmp/build
APPDIR="$BUILD/AppDir"
TOOLS="$BUILD/tools"
APPNAME=gaming-command-center

banner() { echo; echo ">>> $*"; }
die()    { echo "❌ $*" >&2; exit 1; }

banner "Installing the GTK4 + Python runtime (apt)"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-gi python3-gi-cairo python3-yaml \
    gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-1 libadwaita-1-0 \
    librsvg2-common libgdk-pixbuf-2.0-0 \
    wget ca-certificates file desktop-file-utils patchelf dpkg-dev \
    pkg-config libglib2.0-bin libgdk-pixbuf2.0-bin \
    libgtk-4-dev libadwaita-1-dev libgirepository1.0-dev \
  || die "apt install failed"

PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ARCHDIR="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("MULTIARCH") or "x86_64-linux-gnu")')"
banner "Base ready: python $PYVER, multiarch $ARCHDIR"

rm -rf "$BUILD"; mkdir -p "$APPDIR" "$TOOLS" "$OUT"

banner "Fetching linuxdeploy + gtk plugin + appimagetool"
fetch() { [ -x "$2" ] || { wget -q -O "$2" "$1" || die "download $1"; chmod +x "$2"; }; }
fetch "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage" "$TOOLS/linuxdeploy"
fetch "https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh" "$TOOLS/linuxdeploy-plugin-gtk.sh"
fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" "$TOOLS/appimagetool"
export PATH="$TOOLS:$PATH"

banner "Laying out the AppDir + copying the app"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/girepository-1.0" \
         "$APPDIR/usr/share/$APPNAME" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
APP="$APPDIR/usr/share/$APPNAME"
cp "$SRC"/*.py "$SRC/games.yaml" "$SRC/GCC_logo.png" \
   "$SRC/gaming-ccd-helper" "$SRC/gaming-cc-etc-helper" \
   "$SRC/gaming-cc-setup" "$SRC/com.gaming.commandcenter.policy" "$APP/"
chmod +x "$APP/gaming-ccd-helper" "$APP/gaming-cc-etc-helper" "$APP/gaming-cc-setup"

banner "Bundling Python $PYVER (stdlib minus tkinter/test + gi + yaml)"
cp "$(readlink -f "$(command -v python3)")" "$APPDIR/usr/bin/python3"
DEST_LIB="$APPDIR/usr/lib/python$PYVER"
mkdir -p "$DEST_LIB"
cp -a "/usr/lib/python$PYVER/." "$DEST_LIB/"
rm -rf "$DEST_LIB/tkinter" "$DEST_LIB/test" "$DEST_LIB/config-"* "$DEST_LIB"/lib-dynload/_tkinter*.so 2>/dev/null || true
find "$DEST_LIB" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$DEST_LIB/site-packages"
GI_DIR="$(python3 -c 'import gi,os;print(os.path.dirname(gi.__file__))')"
cp -a "$GI_DIR" "$DEST_LIB/site-packages/"
YAML_DIR="$(python3 -c 'import yaml,os;print(os.path.dirname(yaml.__file__))')"
cp -a "$YAML_DIR" "$DEST_LIB/site-packages/"
# also cairo python bindings if gi pulled them
python3 -c 'import cairo,os,sys; sys.stdout.write(os.path.dirname(cairo.__file__))' 2>/dev/null | while read -r d; do
    [ -n "$d" ] && cp -a "$d" "$DEST_LIB/site-packages/" 2>/dev/null || true
done

banner "Copying typelibs"
for t in GLib-2.0 GObject-2.0 Gio-2.0 Gdk-4.0 Gtk-4.0 Adw-1 Pango-1.0 \
         PangoCairo-1.0 cairo-1.0 GdkPixbuf-2.0 Graphene-1.0 HarfBuzz-0.0; do
    f="/usr/lib/$ARCHDIR/girepository-1.0/$t.typelib"
    [ -f "$f" ] && cp "$f" "$APPDIR/usr/lib/girepository-1.0/" || echo "  (skip $t)"
done

banner "Desktop file + 256px icon"
python3 - "$SRC/GCC_logo.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APPNAME.png" <<'PY'
import sys, gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf
GdkPixbuf.Pixbuf.new_from_file_at_scale(sys.argv[1], 256, 256, True).savev(sys.argv[2], 'png', [], [])
PY
cat > "$APPDIR/usr/share/applications/com.gaming.commandcenter.desktop" <<DESKTOP
[Desktop Entry]
Name=Gaming Command Center
Comment=Linux gaming optimization — CPU CCD parking, GPU overclocking, system setup wizard
Exec=python3
Icon=$APPNAME
Terminal=false
Type=Application
StartupWMClass=com.gaming.commandcenter
Categories=Game;System;Utility;
DESKTOP

banner "Deploying libs + GTK runtime (linuxdeploy + gtk plugin, native Debian)"
GI_SO="$(find "$GI_DIR" -maxdepth 1 -name '_gi*.so' | head -1)"
"$TOOLS/linuxdeploy" \
    --appdir "$APPDIR" \
    --executable "$APPDIR/usr/bin/python3" \
    --library "/usr/lib/$ARCHDIR/libgtk-4.so.1" \
    --library "/usr/lib/$ARCHDIR/libadwaita-1.so.0" \
    ${GI_SO:+--library "$GI_SO"} \
    --desktop-file "$APPDIR/usr/share/applications/com.gaming.commandcenter.desktop" \
    --icon-file "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APPNAME.png" \
    --plugin gtk \
  || die "linuxdeploy failed"

banner "Writing AppRun"
rm -f "$APPDIR/AppRun"
cat > "$APPDIR/AppRun" <<APPRUN
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\$0")")"
export APPDIR="\$HERE"
for hook in "\$HERE"/apprun-hooks/*.sh; do [ -r "\$hook" ] && . "\$hook"; done
export PATH="\$HERE/usr/bin:\$PATH"
export LD_LIBRARY_PATH="\$HERE/usr/lib:\$HERE/usr/lib/$ARCHDIR:\${LD_LIBRARY_PATH:-}"
export GI_TYPELIB_PATH="\$HERE/usr/lib/girepository-1.0:\${GI_TYPELIB_PATH:-}"
export PYTHONHOME="\$HERE/usr"
export PYTHONPATH="\$HERE/usr/lib/python$PYVER:\$HERE/usr/lib/python$PYVER/site-packages:\${PYTHONPATH:-}"
export PYTHONDONTWRITEBYTECODE=1
exec "\$HERE/usr/bin/python3" "\$HERE/usr/share/$APPNAME/command-center.py" "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"

banner "Packaging"
OUTFILE="$OUT/Gaming_Command_Center-x86_64.AppImage"
# Write to a temp name and rename into place: if a previous AppImage is still
# running/mounted on the host, the target is "Text file busy" for a direct
# overwrite, but rename() over an in-use file always succeeds.
TMPOUT="$OUT/.Gaming_Command_Center-x86_64.AppImage.$$"
ARCH=x86_64 "$TOOLS/appimagetool" "$APPDIR" "$TMPOUT" || die "appimagetool failed"
mv -f "$TMPOUT" "$OUTFILE" || die "could not move AppImage into place"
echo
echo "✅ Built: $OUTFILE"
