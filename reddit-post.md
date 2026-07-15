# Reddit r/linux_gaming Post Draft

> Post as a text post. **Attach the 4 screenshots** (dashboard, system-doctor,
> games, benchmark) from `assets/screenshots/`. Reply to early comments fast.

## Title (max 300 chars)

Gaming Command Center — an open-source app that scans & fixes common Linux gaming issues, applies per-game Proton fixes, and does Ryzen CCD "Game Mode". Now on the AUR.

<!-- Alt, shorter:
I made an open-source Linux gaming tuning app — system doctor, per-game Proton fixes, Ryzen CCD game mode. Now on the AUR. -->

## Body

Tired of hunting through ProtonDB, Reddit and GitHub issues for the right launch option / governor / fix every time you set up a game? I got tired of it too, so I built **Gaming Command Center** — one open-source GTK4 app that puts it in a single place.

**What it does (v0.1.5):**

- 🩺 **System Doctor** — scans for 15+ common Linux gaming issues (governor, NVIDIA P-state, Coolbits, ReBAR, gamemode.ini, audio power save…) and fixes them in one click
- 📋 **Game fix database** — 121 games in the database (most with documented Proton/Linux fixes); one click sets the launch option or writes the config. Grows by PR, like ProtonDB but "apply it for me". Safe by design: no arbitrary shell commands, ever.
- 🎮 **Ryzen "Game Mode"** — benchmarks each CCD and parks the weaker one with one click, like Ryzen Master's Game Mode but on Linux
- 📈 **Live GPU/CPU monitoring** on the dashboard

Early and honest about it: NVIDIA + AMD Ryzen focused right now. GPU overclocking is the next big feature, not shipped yet.

**Install (Arch/CachyOS/Manjaro):**
```
yay -S gaming-command-center
```
AppImage and source install on GitHub. Any Wayland/X11 desktop with GTK4.

🔗 GitHub: https://github.com/LordHayne/GCC · Site: https://lordhayne.github.io/ · GPL-3.0

It's v0.1.5 and I'm actively building it — I'd love feedback: what games/fixes should go in next, and would you actually use this on your setup?

🐧🎮
