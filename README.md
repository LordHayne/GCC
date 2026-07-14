# Gaming Command Center

A Linux GUI tool for AMD Ryzen CPU + NVIDIA GPU gaming optimization.

![Status](https://img.shields.io/badge/status-WIP-orange) ![License](https://img.shields.io/badge/license-TBD-blue) ![Platform](https://img.shields.io/badge/platform-Linux-blue)

## What it does

Gaming Command Center is a desktop GUI for Linux that brings Ryzen Master-style
CPU controls and NVIDIA overclocking together in one place — something no
existing Linux tool offers.

### Features

**CPU (AMD Ryzen)**
- 🔍 Auto-detects CCD/CCX topology (3900X, 5900X, 7900X, 7950X3D, etc.)
- 🎮 Game Mode: Park weaker CCD with one click (like Ryzen Master Game Mode)
- 📊 CCD Benchmark: Tests which CCD has better silicon (boost clock comparison)
- 📡 Live monitoring: Per-core frequency, online/offline status, temperature
- ⚡ Governor + EPP control: powersave for idle, performance for gaming
- 🤖 GameMode integration: Auto-park CCD when a game starts

**GPU (NVIDIA)**
- 📡 Live monitoring: Core clock, memory clock, power draw, temp, VRAM, P-state
- ⚡ Overclocking: Core offset, VRAM offset sliders (requires Coolbits)
- 🎛️ PowerMizer mode control (Adaptive / Max Performance / Auto)
- 📊 Clock progress bar (current vs max)

### Why?

- **Ryzen Master** is Windows-only
- **Ryzen Master Commander** (existing Linux tool) does fan control + TDP —
  but has no CCD parking, no Game Mode, no GPU controls
- **CoreCtrl** exists but focuses on AMD GPU profiles, not CPU CCD management
- No Linux tool combines CPU CCD parking + GPU OC + Game Mode in one app

### Tech

Currently a Python + GTK4 + libadwaita prototype. The long-term plan is to
rewrite in **Rust + iced** for:
- Native COSMIC DE look (iced is what COSMIC is built on)
- Single binary, zero runtime deps (perfect for AUR)
- Consistent UI across GNOME, KDE, COSMIC, XFCE
- Built-in system tray support

### Current status

Working prototype with:
- CPU topology detection ✅
- CCD parking via pkexec ✅
- Live CPU stats ✅
- CCD benchmark ✅
- GPU monitoring (nvidia-smi) ✅
- GPU overclocking (nvidia-settings) ✅
- PowerMizer control ✅

### Requirements

- Python 3 with PyGObject (GTK4 + libadwaita)
- `sensors` (lm_sensors) for CPU temp
- `nvidia-smi` + `nvidia-settings` for GPU
- `pkexec` (polkit) for CCD parking (root)
- AMD Ryzen CPU with 2+ CCDs
- NVIDIA GPU with Coolbits enabled (for OC)

### Run

```bash
python3 command-center.py
```

### Roadmap

- [ ] System tray icon with status indicator
- [ ] Auto-detect best CCD and recommend which to park
- [ ] GameMode config auto-generation (~/.config/gamemode.ini)
- [ ] Power saving profiles (idle vs gaming auto-switch)
- [ ] Fan control integration (optional)
- [ ] Rust + iced rewrite
- [ ] AUR package
- [ ] Flatpak (if sysfs/pkexec sandbox issues resolved)
- [ ] Support for AMD GPUs (via amdgpu sysfs)
- [ ] CPU undervolting (Curve Optimizer via ryzenadj)
- [ ] Per-game profiles (launch options auto-config)

### Supported CPUs

| CPU | CCDs | Cores per CCD |
|-----|------|---------------|
| Ryzen 9 3900X | 2 | 6+6 |
| Ryzen 9 3950X | 2 | 8+8 |
| Ryzen 9 5900X | 2 | 6+6 |
| Ryzen 9 5950X | 2 | 8+8 |
| Ryzen 9 7900X | 2 | 6+6 |
| Ryzen 9 7950X | 2 | 8+8 |
| Ryzen 9 7900X3D | 2 | 6+6 (asymmetric V-Cache) |
| Ryzen 9 7950X3D | 2 | 8+8 (asymmetric V-Cache) |

Any Ryzen with multiple CCDs is auto-detected.

### License

TBD (probably MIT or GPL)

### Acknowledgements

- [GameMode](https://github.com/FeralInteractive/gamemode) by Feral Interactive
- [Ryzen Master Commander](https://github.com/sam1am/Ryzen-Master-Commander) for inspiration
- All the Linux gaming community pushing Proton + Wayland forward