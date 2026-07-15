# Gaming Command Center 🎮🐧

A Linux GUI tool for gaming system optimization — CPU core management, GPU overclocking, and guided troubleshooting, all in one place.

![Status](https://img.shields.io/badge/status-WIP-orange) ![License](https://img.shields.io/badge/license-GPLv3-blue) ![Platform](https://img.shields.io/badge/platform-Linux-blue)

## What it does

Gaming Command Center is a desktop GUI for Linux that brings hardware optimization and troubleshooting together in one app — something no existing Linux tool offers. Think of it as a mix between AMD Ryzen Master, NVIDIA GeForce Experience, and ProtonDB, but native on Linux.

### Current Features

**CPU — Core Management**
- 🔍 Auto-detects CCD/CCX topology (AMD Ryzen with 2+ CCDs)
- 🎮 Game Mode: Park weaker CCD with one click (like Ryzen Master Game Mode)
- 📊 CCD Benchmark: Tests which CCD has better silicon (silicon lottery)
- 📡 Live monitoring: Per-core frequency, online/offline status, temperature
- ⚡ Governor + EPP control: powersave for idle, performance for gaming

**GPU — Monitoring & Overclocking**
- 📡 Live monitoring: Core clock, memory clock, power draw, temp, VRAM, P-state
- ⚡ Overclocking: Core offset, VRAM offset sliders (requires Coolbits)
- 🎛️ PowerMizer mode control (Adaptive / Max Performance / Auto)

**Game Doctor — Guided Troubleshooting**
- 🩺 System scan: 15+ checks for common Linux gaming issues
- 🔧 Apply Fix buttons: One-click fixes for governor, audio, SATA, modprobe, coolbits, etc.

**Games — Per-Game Fixes**
- 🎮 Detects your Steam games and matches them against a community fix database (`games.yaml`)
- ⚡ One-click apply: sets Steam launch options automatically (backs up your config, refuses while Steam is running)
- 🩹 Shows only the issues relevant to your setup (GPU vendor + Wayland/X11)
- 📊 ProtonDB tier shown per game for context
- 🔒 Safe by design: the database can only carry whitelisted fix types (info, launch option, config file, built-in action) — never arbitrary shell commands

**Setup Wizard — First-run Optimization**
- 🚀 One-click system scan + optimization
- ✅ Checks: GameMode, gamescope, GE-Proton, governor, CCD, NVIDIA driver, ReBAR, Coolbits, modprobe, Wayland, gamemode.ini, SATA, audio, monitor

### Roadmap

**CPU Support**
- ✅ AMD Ryzen (3900X, 5900X, 7900X, 7950X3D, etc.)
- 🔜 Intel CPU support (P-core/E-core management, hybrid architecture)

**GPU Support**
- ✅ NVIDIA (nvidia-smi, nvidia-settings, Coolbits)
- 🔜 AMD GPU support (amdgpu sysfs, CoreCtrl integration)
- 🔜 Intel Arc GPU support

**Game Doctor**
- ✅ System-level checks and fixes
- ✅ Per-game database (games.yaml — community-maintained)
- ✅ Auto-set Steam Launch Options per game
- 🔜 More games (contribute via PR — see below)
- 🔜 Optional auto-update of the database from GitHub
- 🔜 Auto-create GE-Proton user_settings.py
- 🔜 Known bug detection (NVIDIA+Wayland input freeze, P8 idle bug, etc.)

**Platform**
- ✅ Arch Linux / CachyOS
- 🔜 AUR package (yay -S gaming-command-center)
- 🔜 Flatpak (if sysfs/pkexec sandbox issues resolved)
- 🔜 Rust + iced rewrite (native COSMIC DE look, single binary)

### Why?

Linux gaming is growing fast (Steam Deck, Proton, Wayland), but when something breaks, you're on your own — searching Reddit, GitHub issues, and ProtonDB for hours. Gaming Command Center fixes this:

- **Ryzen Master** is Windows-only
- **Ryzen Master Commander** does fan/TDP control but no CCD parking, no Game Mode, no GPU controls
- **CoreCtrl** focuses on AMD GPU profiles, not CPU CCD management
- **ProtonDB** has game fixes but is a website, not built into your system
- No Linux tool combines CPU + GPU + troubleshooting in one app

Gaming Command Center is the missing piece: a **guided assistant** that helps you set up and fix your Linux gaming system, not just a monitor.

### Tech

Currently a Python + GTK4 + libadwaita prototype with a dark Tokyo Night theme. The long-term plan is to rewrite in **Rust + iced** for native COSMIC DE integration and single-binary AUR packaging.

### Install

```bash
git clone https://github.com/LordHayne/GCC.git
cd GCC
./install.sh
```

That's it. The installer detects your distro, installs the GUI dependencies it
needs (asking once before it touches anything), sets up the launcher, icon and
permissions, and verifies the app can actually start before it says "done".
Then launch **Gaming Command Center** from your app menu.

### Supported systems

The installer auto-installs dependencies on the three main distro families and
their derivatives:

| Family | Package manager | Covers (non-exhaustive) |
|--------|-----------------|--------------------------|
| **Arch** | `pacman` | Arch, CachyOS, Manjaro, EndeavourOS, Garuda, Artix |
| **Debian** | `apt` | Debian, Ubuntu, Linux Mint, Pop!\_OS, elementary, Kali |
| **Fedora** | `dnf` | Fedora, Nobara, RHEL, CentOS, Rocky, AlmaLinux |

On any other distro the app still runs — the installer just prints the exact
packages to install by hand (PyGObject, GTK4, libadwaita, PyYAML) instead of
doing it for you.

### Run (without install)

```bash
python3 command-center.py
```

Needs the GUI dependencies below already present. If you're not sure, just run
`./install.sh` — it only installs what's missing.

### Requirements

The installer handles the first two automatically; the rest are feature-specific.

- **Python 3 + PyGObject (GTK4 + libadwaita)** — the app itself *(installed for you)*
- **PyYAML** — for the per-game fix database *(installed for you)*
- `sensors` (lm_sensors) — CPU temperature
- `nvidia-smi` + `nvidia-settings` — GPU monitoring/OC (NVIDIA)
- `pkexec` (polkit) — the one-time `/etc` setup fixes
- AMD Ryzen CPU with 2+ CCDs — for Game Mode / CCD parking
- NVIDIA GPU with Coolbits enabled — for GPU overclocking

### Permissions

Gaming Command Center asks for root in two clearly separated ways, so the thing
you do constantly does not cost you a password:

| What | Needs a password? | Why |
|------|-------------------|-----|
| Game Mode, governor, audio/SATA power saving | **No** | Runtime `sysfs` changes. A reboot undoes all of them, and you toggle Game Mode every time you play. |
| NVIDIA modprobe config, Coolbits | **Yes, once** | These write to `/etc` and survive a reboot. An `xorg.conf` can load arbitrary modules as root, so this stays behind admin authentication. |

The `/etc` fixes are one-time setup steps, and both back up any file they touch
before merging their setting into it — your existing NVIDIA options are kept.

### Contributing game fixes

The whole point is that nobody should have to dig through Reddit and ProtonDB
for two hours again. Found a fix that works? Add it so the next person gets it
with one click.

1. Open [`games.yaml`](games.yaml) — it's commented, with a template at the bottom.
2. Add your game (the `steam_id` is the number in its Steam store URL) and the
   issue + fix.
3. Open a pull request.

Fixes can be one of four types: `info` (show text), `launch_option` (set a Steam
launch option), `file` (write a config file in the user's home), or
`tool_action` (trigger a built-in like Game Mode). **Arbitrary shell commands
are intentionally not supported** — the loader drops anything else, so a bad PR
can't turn the database into an attack vector. If a fix genuinely needs a
command, add it as `info` so the user reads and runs it themselves.

### License

[GPL-3.0-or-later](LICENSE). Forks and redistributions must stay open source,
which is the norm for Linux system tools and keeps the game-fix database
community-owned.

### Acknowledgements

- [GameMode](https://github.com/FeralInteractive/gamemode) by Feral Interactive
- [Ryzen Master Commander](https://github.com/sam1am/Ryzen-Master-Commander) for inspiration
- All the Linux gaming community pushing Proton + Wayland forward