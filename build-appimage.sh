#!/usr/bin/env bash
#
# Gaming Command Center — AppImage builder (Phase 1: native build)
# Copyright (C) 2026 Thomas — GPL-3.0-or-later (see LICENSE)
#
# Bundles the app + Python + GTK4/libadwaita into a single self-contained
# .AppImage. This first phase builds against the HOST libraries — it proves the
# bundling works and the app runs from an AppImage on THIS machine. Cross-distro
# portability (old-glibc container build) is Phase 2.
#
# Usage:  ./build-appimage.sh
# Output: build/Gaming_Command_Center-x86_64.AppImage
#
# The build is verbose on purpose: each stage prints a >>> banner so that when
# something breaks we can see exactly where. Expect to iterate — GTK4 + Python
# is the fiddliest bundling case on Linux.

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD="$SRC/build"
TOOLS="$BUILD/tools"
APPDIR="$BUILD/AppDir"
APPNAME="gaming-command-center"
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# AppImage tools may need FUSE2; extract-and-run avoids that if only FUSE3 exists.
export APPIMAGE_EXTRACT_AND_RUN=1
# linuxdeploy's bundled strip is old and chokes on the modern .relr.dyn ELF
# section that current toolchains emit. Stripping is only a size optimisation,
# so skip it — the libraries are deployed either way.
export NO_STRIP=1

banner() { echo; echo ">>> $*"; }
die()    { echo "❌ $*" >&2; exit 1; }

banner "Build setup  (python $PYVER, source $SRC)"
mkdir -p "$TOOLS"
rm -rf "$APPDIR"
mkdir -p "$APPDIR"

# ── 1. Fetch the build tools (cached in build/tools) ─────────────────────────
fetch() {  # fetch <url> <dest>
    local url="$1" dest="$2"
    if [ ! -x "$dest" ]; then
        echo "  downloading $(basename "$dest")"
        wget -q -O "$dest" "$url" || die "download failed: $url"
        chmod +x "$dest"
    else
        echo "  cached $(basename "$dest")"
    fi
}
banner "Fetching build tools"
fetch "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage" "$TOOLS/linuxdeploy"
fetch "https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh" "$TOOLS/linuxdeploy-plugin-gtk.sh"
fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" "$TOOLS/appimagetool"
export PATH="$TOOLS:$PATH"

# ── 2. Lay out the AppDir skeleton ───────────────────────────────────────────
banner "Creating AppDir layout"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/lib" \
         "$APPDIR/usr/lib/girepository-1.0" \
         "$APPDIR/usr/share/$APPNAME" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# ── 3. Bundle the app itself ─────────────────────────────────────────────────
banner "Copying the application"
APP="$APPDIR/usr/share/$APPNAME"
cp "$SRC"/*.py                             "$APP/"
cp "$SRC/games.yaml"                       "$APP/"
cp "$SRC/GCC_logo.png"                     "$APP/"
cp "$SRC/gaming-ccd-helper"                "$APP/"
cp "$SRC/gaming-cc-etc-helper"             "$APP/"
cp "$SRC/gaming-cc-setup"                  "$APP/"
cp "$SRC/com.gaming.commandcenter.policy"  "$APP/"
chmod +x "$APP/gaming-ccd-helper" "$APP/gaming-cc-etc-helper" "$APP/gaming-cc-setup"

# ── 4. Bundle Python (interpreter + stdlib + gi) ─────────────────────────────
banner "Bundling Python $PYVER"
cp "$(readlink -f "$(command -v python3)")" "$APPDIR/usr/bin/python3"
DEST_LIB="$APPDIR/usr/lib/python$PYVER"
mkdir -p "$DEST_LIB"
# stdlib WITHOUT the system site-packages: that directory holds every unrelated
# system Python package (boost, numpy, …), and linuxdeploy would try to resolve
# their native deps (libmpi, …) which we neither use nor can bundle.
cp -a "/usr/lib/python$PYVER/." "$DEST_LIB/"
rm -rf "$DEST_LIB/site-packages"
find "$DEST_LIB" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Trim stdlib parts the app never uses that would drag in extra native deps or
# break the deploy: Tk/tkinter (→ libtk, the GUI is GTK), the C test modules,
# and the build-time config-*/ artifacts (object files patchelf chokes on).
rm -rf "$DEST_LIB/tkinter" "$DEST_LIB/turtledemo" "$DEST_LIB/turtle.py" \
       "$DEST_LIB/idlelib" "$DEST_LIB/test" "$DEST_LIB/ensurepip" \
       "$DEST_LIB/config-"* 2>/dev/null || true
find "$DEST_LIB/lib-dynload" \( -name '_tkinter*.so' -o -name '_test*.so' \
       -o -name '_ctypes_test*.so' -o -name '_xxtestfuzz*.so' -o -name 'xx*.so' \) \
       -delete 2>/dev/null || true

# Re-add ONLY the third-party packages the app actually imports.
mkdir -p "$DEST_LIB/site-packages"
GI_DIR="$(python3 -c 'import gi, os; print(os.path.dirname(gi.__file__))')"
cp -a "$GI_DIR" "$DEST_LIB/site-packages/"                       # PyGObject (gi)
YAML_DIR="$(python3 -c 'import yaml, os; print(os.path.dirname(yaml.__file__))')"
cp -a "$YAML_DIR" "$DEST_LIB/site-packages/"                     # PyYAML (pure-python)
# PyYAML's optional C extension sits next to the package, not inside it.
SITE_ROOT="$(dirname "$YAML_DIR")"
find "$SITE_ROOT" -maxdepth 1 -name '_yaml*.so' -exec cp {} "$DEST_LIB/site-packages/" \; 2>/dev/null || true

# ── 5. Typelibs (GObject-Introspection) ──────────────────────────────────────
banner "Copying GObject-Introspection typelibs"
for t in GLib-2.0 GObject-2.0 Gio-2.0 Gdk-4.0 Gtk-4.0 Adw-1 Pango-1.0 \
         PangoCairo-1.0 cairo-1.0 GdkPixbuf-2.0 Graphene-1.0 HarfBuzz-0.0; do
    f="/usr/lib/girepository-1.0/$t.typelib"
    [ -f "$f" ] && cp "$f" "$APPDIR/usr/lib/girepository-1.0/" || echo "  (skip missing $t)"
done

# ── 6. Desktop file + icon (needed by linuxdeploy and the AppImage) ──────────
banner "Writing desktop file + icon"
# linuxdeploy validates that an icon in the 256x256 dir is actually 256x256, but
# GCC_logo.png is 1024x1024 — scale it down (GdkPixbuf is on the host already).
python3 - "$SRC/GCC_logo.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APPNAME.png" <<'PY'
import sys, gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf
src, dst = sys.argv[1], sys.argv[2]
GdkPixbuf.Pixbuf.new_from_file_at_scale(src, 256, 256, True).savev(dst, 'png', [], [])
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

# ── 7. Deploy GTK4 + libadwaita + all shared-lib deps via linuxdeploy ─────────
# We drive linuxdeploy for the ELF/shared-library bundling only (its ldd walk +
# rpath fixing is excellent) and do the GTK runtime bits ourselves in step 7b.
# The linuxdeploy-plugin-gtk is Debian-centric and dies on Arch's layout (it
# looks for a /usr/lib/<triplet>/gtk-4.0 modules dir that Arch doesn't have).
banner "Deploying GTK4 / libadwaita / dependencies (linuxdeploy)"
GI_SO="$(find "$GI_DIR" -maxdepth 1 -name '_gi*.so' | head -1)"
"$TOOLS/linuxdeploy" \
    --appdir "$APPDIR" \
    --executable "$APPDIR/usr/bin/python3" \
    --library /usr/lib/libgtk-4.so.1 \
    --library /usr/lib/libadwaita-1.so.0 \
    ${GI_SO:+--library "$GI_SO"} \
    --desktop-file "$APPDIR/usr/share/applications/com.gaming.commandcenter.desktop" \
    --icon-file "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APPNAME.png" \
  || die "linuxdeploy failed (see output above)"

# ── 7b. GTK runtime bits (done manually, distro-agnostic) ────────────────────
banner "Adding GTK runtime data (schemas + pixbuf loaders)"
# GLib schemas — GTK/libadwaita read settings from the compiled schema cache.
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp /usr/share/glib-2.0/schemas/gschemas.compiled \
   "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || echo "  (no gschemas.compiled — GTK may warn, not fatal)"
# GDK-Pixbuf loaders — PNG is built into libgdk_pixbuf on Arch (our logo loads
# without these), but copy any external loaders (e.g. SVG for the nav icons)
# and regenerate a cache with bundle-relative paths so it stays self-contained.
PB_SRC="/usr/lib/gdk-pixbuf-2.0/2.10.0"
if [ -d "$PB_SRC/loaders" ]; then
    PB_DST="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
    mkdir -p "$PB_DST"
    cp "$PB_SRC/loaders/"*.so "$PB_DST/" 2>/dev/null || true
    if command -v gdk-pixbuf-query-loaders >/dev/null 2>&1; then
        ( cd "$PB_DST" && GDK_PIXBUF_MODULEDIR="$PB_DST" gdk-pixbuf-query-loaders ./*.so 2>/dev/null ) \
            | sed "s|$APPDIR|.|g" > "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" || true
    fi
fi

# ── 8. Custom AppRun: set env, source the gtk plugin hooks, launch our app ────
banner "Writing AppRun"
# linuxdeploy left an AppRun symlink → usr/bin/python3; remove it first, or the
# heredoc below would write THROUGH the symlink and clobber the python binary.
rm -f "$APPDIR/AppRun"
cat > "$APPDIR/AppRun" <<APPRUN
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\$0")")"
export APPDIR="\$HERE"
export PATH="\$HERE/usr/bin:\$PATH"
export LD_LIBRARY_PATH="\$HERE/usr/lib:\${LD_LIBRARY_PATH:-}"
export GI_TYPELIB_PATH="\$HERE/usr/lib/girepository-1.0:\${GI_TYPELIB_PATH:-}"
export GSETTINGS_SCHEMA_DIR="\$HERE/usr/share/glib-2.0/schemas:\${GSETTINGS_SCHEMA_DIR:-}"
export XDG_DATA_DIRS="\$HERE/usr/share:\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GDK_PIXBUF_MODULE_FILE="\$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
export PYTHONHOME="\$HERE/usr"
export PYTHONPATH="\$HERE/usr/lib/python$PYVER:\$HERE/usr/lib/python$PYVER/site-packages:\${PYTHONPATH:-}"
export PYTHONDONTWRITEBYTECODE=1
exec "\$HERE/usr/bin/python3" "\$HERE/usr/share/$APPNAME/command-center.py" "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# ── 9. Package ───────────────────────────────────────────────────────────────
banner "Packaging the AppImage"
OUT="$BUILD/Gaming_Command_Center-x86_64.AppImage"
ARCH=x86_64 "$TOOLS/appimagetool" "$APPDIR" "$OUT" || die "appimagetool failed"

echo
echo "✅ Built: $OUT"
echo "   Test it:  chmod +x \"$OUT\" && \"$OUT\""
