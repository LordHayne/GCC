# Ryzen CCD Controller — Konzept

## Vision
Ein grafisches Linux-Tool für AMD Ryzen CPUs mit mehreren CCDs (3900X, 5900X, 7900X, 7950X3D, etc.)
das позволяет: CCDs zu parken, den besseren CCD zu finden, und Game Mode automatisch zu verwalten.

## Zielgruppe
- Linux-Gamer mit Ryzen CPUs (große und wachsende Community)
- Bisher gibt es KEIN grafisches Tool dafür auf Linux
- Ryzen Master ist Windows-only
- Aktuell muss man sysfs-Skripte manuell zusammenbauen

## Features

### Core
1. **CPU-Topologie-Erkennung** — CCD/CCX Layout automatisch auslesen (L3 cache shared_cpu_list)
2. **CCD-Benchmark** — Silikon-Lotterie testen: welcher CCD hat besseren Boost? (openssl oder stress-ng pro CCD)
3. **CCD-Parking Toggle** — Mit einem Klick CCD1 parken/aktivieren (pkexec für root)
4. **Live-Status** — Kerne online/offline, Frequenz, Temperatur, TDP — live aktualisiert
5. **GameMode-Integration** — Automatisch CCD parken wenn Spiel startet, reaktivieren beim Beenden
6. **Stromspar-Profil** — CPU Governor powersave im Idle, performance beim Zocken
7. **System-Tray Icon** — Status immer sichtbar, Umschalten aus dem Tray

### GUI
- Hauptfenster mit CPU-Diagramm (CCD0 / CCD1 Visualisierung)
- Pro CCD: Kerne, Frequenz, Temperatur, L3-Cache
- Button: "Game Mode ON/OFF" mit klarem Status-Feedback
- Benchmark-Button: "CCD-Performance testen"
- Settings: GameMode-Config automatisch schreiben (~/.config/gamemode.ini)
- Auto-Detect: Welcher CCD ist besser → Empfehlung anzeigen

### Unter der Haube
- Liest: /sys/devices/system/cpu/cpu*/online, cpufreq, cache/index3/shared_cpu_list
- Schreibt (via pkexec): /sys/devices/system/cpu/cpu*/online
- Konfiguriert: ~/.config/gamemode.ini (park_cores, pin_cores)
- Liest sensors: k10temp (Tdie/Tctl), nvidia-smi (GPU power)
- systemd service für Stromspar-Profile

## Supported CPUs (auto-detect)
- Ryzen 9 3900X (2x CCD, 6+6)
- Ryzen 9 3950X (2x CCD, 8+8)
- Ryzen 9 5900X (2x CCD, 6+6)
- Ryzen 9 5950X (2x CCD, 8+8)
- Ryzen 9 7900X (2x CCD, 6+6)
- Ryzen 9 7950X (2x CCD, 8+8)
- Ryzen 9 7900X3D (2x CCD, 6+6, V-Cache asymmetrisch)
- Ryzen 9 7950X3D (2x CCD, 8+8, V-Cache asymmetrisch)
- Jeder Ryzen mit >1 CCD

## Distribution
- AUR Package (ryzen-ccd-controller)
- Optional: Flatpak (mit polkit für root-Zugriff)
- GitHub Release (binary)

## Tech Stack (Vorschlag)
- **Backend:** Rust (sysfs access, pkexec, sensors, gamemode config)
- **Frontend:** Tauri (HTML/CSS/JS UI) oder iced (Rust-native)
- **Packaging:** AUR (PKGBUILD), GitHub Releases
- **Polkit:** Eigene .policy-Datei für CCD-Parking (wie gamemode)