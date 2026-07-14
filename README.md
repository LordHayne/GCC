# Gaming Command Center 🎮🐧

A Linux GUI tool for gaming system optimization — CPU core management, GPU overclocking, and guided troubleshooting, all in one place.

![Status](https://img.shields.io/badge/status-WIP-orange) ![License](https://img.shields.io/badge/license-TBD-blue) ![Platform](https://img.shields.io/badge/platform-Linux-blue)

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
- 🎮 Per-game fixes: Coming soon — known issues and optimal config per game (like ProtonDB, but machine-readable and built into the app)

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
- 🔜 Per-game database (games.yaml — community-maintained, like ProtonDB)
- 🔜 Auto-set Steam Launch Options per game
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

Then launch "Gaming Command Center" from your app menu.

### Run (without install)

```bash
python3 command-center.py
```

### Requirements

- Python 3 with PyGObject (GTK4 + libadwaita)
- `sensors` (lm_sensors) for CPU temp
- `nvidia-smi` + `nvidia-settings` for GPU (NVIDIA)
- `pkexec` (polkit) for root operations
- AMD Ryzen CPU with 2+ CCDs (for Game Mode)
- NVIDIA GPU with Coolbits enabled (for OC)

### License

TBD (probably MIT or GPL)

### Acknowledgements

- [GameMode](https://github.com/FeralInteractive/gamemode) by Feral Interactive
- [Ryzen Master Commander](https://github.com/sam1am/Ryzen-Master-Commander) for inspiration
- All the Linux gaming community pushing Proton + Wayland forward