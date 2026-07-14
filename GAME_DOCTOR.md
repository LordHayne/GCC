# Gaming Command Center — Game Doctor Feature

## Problem
Linux-Gaming funktioniert gut, aber wenn etwas schiefgeht muss man sich
jede Lösung mühsam aus Foren, Reddit und GitHub Issues zusammensuchen.
Niemand führt einen durch die Fehlerbehebung.

## Lösung: Game Doctor

Ein geführter Fehlerbehebungs-Assistent der:
1. Das System scannt und bekannte Probleme erkennt
2. Schritt-für-Schritt Lösungen anbietet (mit "Fix anwenden" Button)
3. Pro-Spiel bekannte Probleme und optimale Konfiguration kennt

### System-Scan (automatisch beim Start)

| Check | Erkennung | Lösungsvorschlag |
|-------|-----------|-------------------|
| NVIDIA+Wayland + Spiel friert ein | Driver + Session-Typ + Spiele-Liste | gamescope Launch Option hinzufügen |
| GPU stuck bei P8 | nvidia-smi pstate + clock | nvidia-smi -lgc setzen |
| ReBAR nur 256MB | nvidia-smi -q | "Hardware-Limit RTX 20-series, BIOS Small BAR aktivieren" |
| Governor immer performance | /sys/.../scaling_governor | powersave + EPP power setzen |
| Kein GameMode installiert | which game-performance | "pacman -S gamemode" vorschlagen |
| Kein gamescope | which gamescope | "pacman -S gamescope" vorschlagen |
| Falsche Proton-Version | Steam config auslesen | GE-Proton Download vorschlagen |
| GE-Proton user_settings.py fehlt | Datei-Check | Datei erstellen mit korrekten Settings |
| NVreg_PreserveVideoMemoryAllocations | /etc/modprobe.d/ | Fix anzeigen |
| SMT an trotz 2 CCD | smt/control | CCD-Parking vorschlagen |
| SATA Link Power max_performance | /sys/class/scsi_host/ | med_power_with_dipm setzen |

### Per-Game Database

Jedes Spiel hat einen Eintrag mit:
- Bekannte Probleme auf Linux
- Empfohlene Launch Options
- Empfohlene Proton-Version
- Bekannte Bugs / Workarounds

```yaml
# games.yaml — Beispiel
overwatch_2:
  name: "Overwatch 2"
  steam_id: 2357570
  known_issues:
    - symptom: "Mus friert ein / kann sich nicht 360° drehen"
      cause: "NVIDIA+Wayland Input Grabbing Bug"
      fix:
        type: "launch_option"
        value: "gamescope -W 2560 -H 1440 -r 144 -f --immediate-flips -- game-performance %command%"
        auto_apply: true
    - symptom: "GPU bleibt auf P8 (555 MHz)"
      cause: "NVIDIA Power State Bug"
      fix:
        type: "command"
        value: "sudo nvidia-smi -lgc 1815,1815"
        auto_apply: false
    - symptom: "Stuttering bei VRAM-heavy Spielen"
      cause: "ReBAR nur 256MB (RTX 20-series Hardware-Limit)"
      fix:
        type: "info"
        value: "Im BIOS 'Small BAR' / 'Above 4G Decoding' aktivieren. RTX 20-series ist auf 256MB begrenzt — keine VBIOS-Lösung existiert."
        auto_apply: false

hydroneer:
  name: "Hydroneer"
  steam_id: 1106840
  known_issues:
    - symptom: "Maus kann sich nicht 360° drehen"
      cause: "NVIDIA+Wayland ohne gamescope"
      fix:
        type: "launch_option"
        value: "gamescope -W 2560 -H 1440 -r 144 -f --immediate-flips -- game-performance %command%"
        auto_apply: true

# Template für neue Spiele:
_template:
  known_issues:
    - symptom: "Allgemeine Performance schlecht"
      check: "governor == performance"
      cause: "CPU Governor steht immer auf performance (Stromverschwendung)"
      fix:
        type: "info"
        value: "Im Idle powersave nutzen — GameMode schaltet beim Zocken automatisch um"
    - symptom: "CCD-Parking nicht aktiv"
      check: "ccd_count > 1 and not game_mode"
      cause: "Bei Ryzen mit 2 CCDs kann CCD1 geparkt werden"
      fix:
        type: "tool_action"
        value: "park_ccd"
        auto_apply: true
```

### UI Konzept

```
┌──────────────────────────────────────────────┐
│  🩺 Game Doctor                                │
│                                                │
│  System-Scan...                                │
│  ✅ GameMode installiert                       │
│  ✅ gamescope installiert                      │
│  ⚠️  CPU Governor: performance (sollte         │
│      powersave sein)              [Fix anwenden]│
│  ⚠️  NVIDIA P8 idle (555 MHz)     [Fix anwenden]│
│  ℹ️  ReBAR: 256MB (Hardware-Limit)             │
│  ✅ GE-Proton erkannt                           │
│  ⚠️  user_settings.py fehlt       [Erstellen]  │
│                                                │
│  ──────────────────────────────────────────   │
│  Spiele-Profile                                │
│                                                │
│  [Overwatch 2 ▼]                              │
│  ⚠️  Maus friert ein auf NVIDIA+Wayland         │
│      → gamescope Launch Option setzen         │
│      [Launch Option hinzufügen]                │
│                                                │
│  ⚠️  GPU bleibt auf P8 (555 MHz)                │
│      → GPU Lock Clocks setzen                  │
│      [Fix anwenden]                            │
│                                                │
│  [Hydroneer ▼]                                 │
│  ✅ Keine bekannten Probleme                   │
└──────────────────────────────────────────────┘
```

### Auto-Fix Kategorien

1. **Launch Options** — Setzt Steam Launch Options für ein Spiel
   - gamescope commands
   - game-performance %command%
   - DXVK/VKD3D env vars

2. **System Settings** — Ändert Systemeinstellungen
   - Governor/EPP
   - SATA Link Power
   - PCIe Runtime PM
   - USB Auto-suspend

3. **File Creation** — Erstellt Konfigurationsdateien
   - GE-Proton user_settings.py
   - ~/.config/gamemode.ini
   - /etc/modprobe.d/nvidia.conf

4. **Package Install** — Installiert fehlende Pakete
   - gamemode, gamescope, GE-Proton

5. **Info Only** — Zeigt Info ohne Fix
   - ReBAR Hardware-Limits
   - BIOS-Einstellungen

### Datenquelle

- Community-maintained games.yaml (wie ProtonDB aber maschinenlesbar)
- Beiträge via GitHub PRs
- Auto-Update der Datenbank beim Start