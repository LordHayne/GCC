#!/usr/bin/env python3
#
# Gaming Command Center — Linux gaming system optimisation
# Copyright (C) 2026 Thomas
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file, or <https://www.gnu.org/licenses/>.
#
"""Gaming Command Center — System Scanner for Setup Wizard"""
import subprocess, os, re, shutil
from topology import CPUTopology, format_cpu_list

class SystemCheck:
    """Single system check with status + fix."""

    def __init__(self, name, check_fn, fix_fn=None, info_only=False):
        self.name = name
        self.check_fn = check_fn
        self.fix_fn = fix_fn
        self.info_only = info_only
        self.status = None  # "ok", "warning", "info", "fixing"
        self.message = ""
        self.fix_message = ""
        self.run()

    def run(self):
        try:
            self.status, self.message, self.fix_message = self.check_fn()
        except Exception as e:
            self.status = "info"
            self.message = f"Check error: {e}"
            self.fix_message = ""

    def apply_fix(self):
        if not self.fix_fn or self.info_only:
            return False
        try:
            return self.fix_fn()
        except:
            return False


def scan_system():
    """Scans the system and returns list of SystemCheck objects."""
    checks = []

    # === Gaming Tools ===

    # 1. GameMode installed?
    def check_gamemode():
        if shutil.which("game-performance"):
            return "ok", "GameMode installed ✅", ""
        return "warning", "GameMode NOT installed", "pacman -S gamemode"
    checks.append(SystemCheck("GameMode", check_gamemode))

    # 2. gamescope installed?
    def check_gamescope():
        if shutil.which("gamescope"):
            return "ok", "gamescope installed ✅", ""
        return "warning", "gamescope NOT installed", "pacman -S gamescope"
    checks.append(SystemCheck("gamescope", check_gamescope))

    # 3. GE-Proton installed?
    def check_geproton():
        proton_dir = os.path.expanduser("~/.steam/root/compatibilitytools.d")
        if os.path.exists(proton_dir):
            versions = os.listdir(proton_dir)
            ge_versions = [v for v in versions if "GE-Proton" in v]
            if ge_versions:
                return "ok", f"GE-Proton installed ({ge_versions[0]}) ✅", ""
            return "warning", "GE-Proton NOT installed", "Download GE-Proton from GitHub and extract to ~/.steam/root/compatibilitytools.d/"
        return "warning", "GE-Proton NOT installed", "Download GE-Proton from GitHub"
    checks.append(SystemCheck("GE-Proton", check_geproton))

    # === CPU ===

    # 4. CPU Governor
    def check_governor():
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as f:
                gov = f.read().strip()
            if gov == "powersave":
                return "ok", "CPU Governor: powersave ✅ (optimal for idle)", ""
            elif gov == "performance":
                return "warning", "CPU Governor: performance (should be powersave)", "Set to powersave — GameMode will switch to performance when gaming"
            return "info", f"CPU Governor: {gov}", ""
        except:
            return "info", "Could not read governor", ""
    checks.append(SystemCheck("CPU Governor", check_governor))

    # 5. CCD count + Game Mode
    def check_ccd():
        topo = CPUTopology()
        count = topo.ccd_count()
        if count < 2:
            return "info", f"{count} CCD — Game Mode not available (requires 2+ CCDs)", ""

        parked = topo.get_parked_ccds()
        if parked:
            names = ", ".join(f"CCD{c}" for c in parked)
            return "ok", f"{count} CCDs — Game Mode ACTIVE ({names} parked) ✅", ""

        keep = topo.keep_ccd()
        park = topo.park_plan(keep)
        return ("warning",
                f"{count} CCDs detected — Game Mode not active",
                f"Park CPUs {format_cpu_list(park)} so CCD{keep} "
                f"({topo.core_count(keep)} cores) gets exclusive cache and boost headroom")
    checks.append(SystemCheck("CCD / Game Mode", check_ccd))

    # === GPU ===

    # 6. NVIDIA driver
    def check_nvidia():
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=3)
            ver = r.stdout.strip()
            if ver:
                return "ok", f"NVIDIA driver {ver} ✅", ""
            return "warning", "NVIDIA driver not detected", ""
        except:
            return "info", "Could not check NVIDIA driver", ""
    checks.append(SystemCheck("NVIDIA Driver", check_nvidia))

    # 7. NVIDIA P-State (P8 idle bug)
    def check_pstate():
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=pstate,clocks.gr", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=3)
            parts = r.stdout.strip().split(", ")
            if len(parts) >= 2:
                pstate = parts[0]
                clock = parts[1]
                if pstate == "P8" and "555" in clock:
                    return "warning", f"GPU stuck at {pstate} ({clock}) — known NVIDIA bug", "nvidia-smi -lgc 1815,1815 (fix GPU clock)"
                return "ok", f"GPU P-State: {pstate} ({clock}) ✅", ""
        except:
            pass
        return "info", "Could not check GPU P-State", ""
    checks.append(SystemCheck("GPU P-State", check_pstate))

    # 8. ReBAR
    def check_rebar():
        try:
            r = subprocess.run(["nvidia-smi", "-q", "-d", "MEMORY"],
                              capture_output=True, text=True, timeout=3)
            if "BAR1 Memory" in r.stdout:
                m = re.search(r'BAR1 Memory.*?Total.*??(\d+)\s*MiB', r.stdout, re.DOTALL)
                if m:
                    bar = int(m.group(1))
                    if bar >= 256:
                        if bar <= 256:
                            return "info", f"ReBAR: {bar}MB (Hardware limit RTX 20-series, no VBIOS fix exists)", ""
                        return "ok", f"ReBAR: {bar}MB ✅", ""
            r2 = subprocess.run(["nvidia-smi", "-q"], capture_output=True, text=True, timeout=3)
            if "BAR1" in r2.stdout:
                for line in r2.stdout.split("\n"):
                    if "BAR1" in line and "Total" in line:
                        m = re.search(r'(\d+)\s*MiB', line)
                        if m:
                            bar = int(m.group(1))
                            if bar <= 256:
                                return "info", f"ReBAR: {bar}MB (Hardware limit RTX 20-series, no fix)", ""
                            return "ok", f"ReBAR: {bar}MB ✅", ""
            return "info", "ReBAR status unclear", "Enable 'Above 4G Decoding' + 'Re-Size BAR Support' in BIOS"
        except:
            return "info", "Could not check ReBAR", ""
    checks.append(SystemCheck("ReBAR", check_rebar, info_only=True))

    # 9. Coolbits (OC support)
    def check_coolbits():
        try:
            r = subprocess.run(["nvidia-settings", "-q", "GPUGraphicsClockOffsetAllPerformanceLevels"],
                              capture_output=True, text=True, timeout=3)
            if "range" in r.stdout.lower() or ": 0" in r.stdout:
                return "ok", "Coolbits enabled — GPU overclocking available ✅", ""
            return "warning", "Coolbits NOT enabled", "nvidia-xconfig --cool-bits=28 or edit Xorg config"
        except:
            return "info", "Could not check Coolbits", ""
    checks.append(SystemCheck("Coolbits / GPU-OC", check_coolbits))

    # === System ===

    # 10. NVreg_PreserveVideoMemoryAllocations
    def check_nvreg():
        path = "/etc/modprobe.d/nvidia.conf"
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if "NVreg_PreserveVideoMemoryAllocations=1" in content:
                return "ok", "NVreg_PreserveVideoMemoryAllocations enabled ✅", ""
            return "warning", "NVreg_PreserveVideoMemoryAllocations missing", "Add 'options nvidia NVreg_PreserveVideoMemoryAllocations=1' to /etc/modprobe.d/nvidia.conf"
        return "warning", "/etc/modprobe.d/nvidia.conf missing", "Create with 'options nvidia NVreg_PreserveVideoMemoryAllocations=1'"
    checks.append(SystemCheck("NVIDIA Modprobe", check_nvreg))

    # 11. Wayland or X11?
    def check_session():
        wayland = os.environ.get("WAYLAND_DISPLAY", "")
        x11 = os.environ.get("DISPLAY", "")
        if wayland:
            return "ok", f"Wayland active ({wayland}) ✅", ""
        elif x11:
            return "info", f"X11 active ({x11})", "Wayland is recommended for NVIDIA gaming"
        return "info", "Session type unclear", ""
    checks.append(SystemCheck("Display Session", check_session))

    # 12. gamemode.ini
    def check_gamemode_ini():
        path = os.path.expanduser("~/.config/gamemode.ini")
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if "park_cores" in content:
                return "ok", "gamemode.ini with CCD config present ✅", ""
            return "info", "gamemode.ini exists but without CCD config", "Add park_cores + pin_cores for automatic CCD parking when gaming"
        return "warning", "gamemode.ini missing", "Create with park_cores + pin_cores for automatic CCD parking when gaming"
    checks.append(SystemCheck("GameMode Config", check_gamemode_ini))

    # 13. SATA Link Power
    def check_sata():
        try:
            for i in range(4):
                path = f"/sys/class/scsi_host/host{i}/link_power_management_policy"
                if os.path.exists(path):
                    with open(path) as f:
                        policy = f.read().strip()
                    if policy == "max_performance":
                        return "warning", f"SATA Link Power: {policy} (wastes power)", "Set to med_power_with_dipm"
                    return "ok", f"SATA Link Power: {policy} ✅", ""
            return "info", "No SATA controllers found", ""
        except:
            return "info", "Could not check SATA", ""
    checks.append(SystemCheck("SATA Link Power", check_sata))

    # 14. Audio Power Save
    def check_audio():
        path = "/sys/module/snd_hda_intel/parameters/power_save"
        if os.path.exists(path):
            with open(path) as f:
                val = f.read().strip()
            if val == "1":
                return "ok", "Audio Power Save enabled ✅", ""
            return "warning", f"Audio Power Save: {val} (should be 1)", "echo 1 > /sys/module/snd_hda_intel/parameters/power_save"
        return "info", "Audio Power Save not available", ""
    checks.append(SystemCheck("Audio Power Save", check_audio))

    # 15. Monitor refresh rate
    def check_refresh():
        try:
            r = subprocess.run(["xrandr" if os.environ.get("DISPLAY") else "wlr-randr"],
                              capture_output=True, text=True, timeout=3)
            if "144" in r.stdout or "120" in r.stdout or "165" in r.stdout:
                return "ok", "High-refresh monitor detected ✅", ""
            if r.stdout:
                return "info", "Monitor detected", ""
        except:
            pass
        return "info", "Could not check monitor", ""
    checks.append(SystemCheck("Monitor", check_refresh))

    return checks


if __name__ == "__main__":
    print("=== Gaming Command Center — System Scan ===\n")
    checks = scan_system()
    ok = sum(1 for c in checks if c.status == "ok")
    warn = sum(1 for c in checks if c.status == "warning")
    info = sum(1 for c in checks if c.status == "info")
    for c in checks:
        icon = {"ok": "✅", "warning": "⚠️", "info": "ℹ️"}[c.status]
        print(f"{icon} {c.name}: {c.message}")
        if c.fix_message and c.status == "warning":
            print(f"   → Fix: {c.fix_message}")
    print(f"\n=== Summary: {ok} OK, {warn} Warnings, {info} Info ===")