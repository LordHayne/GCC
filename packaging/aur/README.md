# AUR packaging — `gaming-command-center`

Native, source-based PKGBUILD that installs Gaming Command Center from the
tagged GitHub release. Unlike the AppImage, this wires the two privileged
helpers and the polkit policy straight into the system at install time, so the
app's first-run setup wizard never appears — `pacman -S` *is* the setup.

## Files

- `PKGBUILD` — the recipe (native, `arch=any`).
- `.SRCINFO` — generated metadata the AUR requires. Regenerate after any edit:
  `makepkg --printsrcinfo > .SRCINFO`

## What it does differently from upstream `install.sh`

A pacman package owns `/usr`, not `/usr/local`. `prepare()` relocates the two
helpers from the `/usr/local/bin` paths the source hardcodes to `/usr/bin`, and
patches every reference in lockstep — the app's `CCD_HELPER_PATH` /
`ETC_HELPER_PATH` constants **and** the polkit policy's `exec.path`
annotations — so the no-password Game-Mode action still resolves and
`needs_setup()` returns false on a packaged install.

## Test locally before publishing

```bash
cd packaging/aur
makepkg -si          # build + install, pulls deps
gaming-command-center
```

Confirm: the app launches, no first-run setup modal appears, and Game Mode /
the /etc fixes work without re-running any installer.

## Publish to the AUR

1. Create an account on https://aur.archlinux.org and add your SSH public key
   (Account → My Account → SSH Public Key).
2. Clone the (empty) package repo — the name reserves it on first push:
   ```bash
   git clone ssh://aur@aur.archlinux.org/gaming-command-center.git aur-gcc
   cd aur-gcc
   cp ../gaming-command-center/packaging/aur/PKGBUILD .
   cp ../gaming-command-center/packaging/aur/.SRCINFO .
   git add PKGBUILD .SRCINFO
   git commit -m "Initial import: gaming-command-center 0.1.5-1"
   git push
   ```
3. The package is then live at
   `https://aur.archlinux.org/packages/gaming-command-center` and installable
   with any AUR helper (`yay -S gaming-command-center`, `paru -S …`).

## On each new release

1. `pkgver=` → new version, reset `pkgrel=1`.
2. Update `sha256sums` for the new tag tarball:
   `updpkgsums` (from `pacman-contrib`) or `makepkg -g`.
3. `makepkg --printsrcinfo > .SRCINFO`
4. Commit both files and push. AUR helpers pick it up automatically.
