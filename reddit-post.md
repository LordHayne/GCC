# Reddit r/linux_gaming Post Draft

## Title (max 300 chars)

Gaming Command Center — open-source Linux app for GPU overclocking, CPU optimization, one-click game fixes, and system diagnostics

## Body

Hey everyone,

I've been gaming on Linux for a while now, and the one thing that always frustrated me was how much manual setup it takes — hunting through ProtonDB, Reddit threads, and GitHub issues to find the right launch option, governor setting, or gamescope flags for each game.

So I'm building **Gaming Command Center** — an open-source GTK4 app that brings it all together in one place:

### What it does

**🩺 System Doctor**
- Scans your system for 15+ common Linux gaming issues
- Shows what's wrong and offers one-click fixes
- Covers: governor settings, NVIDIA P-state bug, Coolbits, ReBAR, gamemode.ini, audio power save, Wayland/X11 detection, and more
- Works on NVIDIA and AMD GPUs, any CPU

**📋 Game Fix Database**
- Community-maintained YAML database of known Linux/Proton issues per game
- One-click apply: sets Steam launch options, writes config files, or shows you what to do
- 24 games seeded so far (Overwatch 2, Cyberpunk 2077, Elden Ring, BG3, Helldivers 2, Palworld, CS2, RDR2, and more)
- Grows by pull request — like ProtonDB, but machine-readable and with "apply it for me"
- Security: only 4 fix types allowed (info, launch_option, file, tool_action) — no arbitrary shell commands ever

**⚡ GPU Overclocking (NVIDIA)**
- Core/VRAM offset sliders via nvidia-settings (requires Coolbits)
- PowerMizer mode control
- Live monitoring: clock, power, temp, VRAM, P-state, utilization

**🎮 CPU Optimization**
- Detects CPU topology automatically (no hardcoded CPU numbers)
- For AMD Ryzen with 2+ CCDs: benchmarks each CCD to find the better silicon (silicon lottery), then parks the weaker one with one click — like Ryzen Master's Game Mode, but on Linux
- For single-CCD or Intel CPUs: Game Mode is hidden, rest of the app works normally
- No password needed (polkit policy included)

**📊 CCD Benchmark**
- Tests each core individually with live progress
- Shows which CCD has better sustained boost clocks
- Marks the best CCD with a badge

### Design
- Dark "Tokyo Night" theme, built with GTK4 + libadwaita
- Sidebar navigation: Dashboard, Games, System Doctor, Benchmark, Settings
- Works on any Wayland desktop (COSMIC, GNOME, KDE, etc.)

### Supported hardware & distros
- **CPU**: Any CPU — full features on AMD Ryzen 2+ CCD, basic monitoring on others
- **GPU**: NVIDIA (overclocking via nvidia-settings) — monitoring works on any GPU
- **Distro**: Arch, CachyOS, Manjaro, Ubuntu, Debian, Fedora, Nobara (auto-detects package manager)
- **Desktop**: Any Wayland or X11 desktop with GTK4

### Install
```bash
git clone https://github.com/LordHayne/GCC.git
cd GCC
./install.sh
```
Then launch "Gaming Command Center" from your app menu.

### Roadmap
- AUR package
- AMD GPU support (amdgpu sysfs for overclocking + monitoring)
- Intel CPU support
- System tray icon
- Auto-setup wizard ("one click, everything optimized")
- Rust + iced rewrite (native COSMIC look, single binary)

### Contributing
The game fix database (games.yaml) grows with the community. Found a fix for a game? Add it to the YAML and open a PR — the app validates everything on load, so a bad entry can never execute arbitrary commands.

GitHub: https://github.com/LordHayne/GCC

License: GPL-3.0

Feedback welcome! I'd especially love to hear:
- What games should we add to the fix database next?
- What system issues should the System Doctor check for?
- Would you use this on your setup?

🐧🎮