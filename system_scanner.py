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
import subprocess, os, re, shutil, resource
from topology import CPUTopology, format_cpu_list


def _nvidia_smi(args, timeout=3):
    """Run nvidia-smi once and hand back (returncode, stdout). Returns
    (None, "") when the binary is missing or crashes — callers treat that as
    'no NVIDIA GPU here', never as success."""
    try:
        r = subprocess.run(["nvidia-smi", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return None, ""


def _nvml_broken(rc, out):
    """A non-zero exit or an NVML error in the OUTPUT (nvidia-smi prints the
    'Driver/library version mismatch' line to *stdout* and still fills it) means
    the driver stack is not usable right now."""
    low = out.lower()
    return rc is None or rc != 0 or "failed to initialize nvml" in low or "mismatch" in low


def _read1(path):
    """Read a one-line sysfs file, or None if it's absent/unreadable."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _drm_drivers():
    """Kernel drivers bound to the DRM cards — the vendor-neutral way to tell
    which GPU is actually in use: 'nvidia', 'amdgpu', 'i915' or 'xe'."""
    drivers = set()
    try:
        for card in os.listdir("/sys/class/drm"):
            if re.fullmatch(r"card\d+", card):
                link = os.path.realpath(f"/sys/class/drm/{card}/device/driver")
                drivers.add(os.path.basename(link))
    except OSError:
        pass
    return drivers


class SystemCheck:
    """Single system check with status + fix."""

    def __init__(self, name, check_fn, fix_fn=None, info_only=False):
        self.name = name
        self.check_fn = check_fn
        self.fix_fn = fix_fn
        self.info_only = info_only
        self.status = None  # "critical", "ok", "warning", "info", "fixing"
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

    # Probe the NVIDIA stack ONCE so every GPU check agrees on whether the
    # driver is usable. Without this, a driver/library mismatch made each GPU
    # check fail on its own with a different vague message.
    have_nvidia = bool(shutil.which("nvidia-smi"))
    nv_rc, nv_out = _nvidia_smi(["--query-gpu=driver_version", "--format=csv,noheader"])
    nvml_ok = have_nvidia and not _nvml_broken(nv_rc, nv_out)

    # Vendor-neutral GPU detection so AMD/Intel machines get the right checks and
    # NVIDIA-only ones stay out of their way.
    gpu_drivers = _drm_drivers()
    have_amd_gpu = "amdgpu" in gpu_drivers

    # === System Health ===

    # 0. Pending reboot — the running kernel's modules were removed by an update.
    #    This is the root cause of most "driver/library version mismatch" reports,
    #    so it comes first and is the only CRITICAL check.
    def check_reboot():
        rel = os.uname().release
        if not (os.path.isdir(f"/usr/lib/modules/{rel}") or
                os.path.isdir(f"/lib/modules/{rel}")):
            return ("critical",
                    f"Running kernel {rel} has no modules on disk — the system was "
                    f"updated since boot",
                    "Reboot to load the new kernel & GPU driver modules")
        return "ok", f"Kernel {rel} — no pending reboot ✅", ""
    checks.append(SystemCheck("Reboot / Kernel", check_reboot))

    # === Gaming Tools ===

    # 1. GameMode installed?
    def check_gamemode():
        # game-performance is GameMode ≥ 1.8; gamemoderun covers older versions.
        if shutil.which("game-performance") or shutil.which("gamemoderun"):
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
        # Cover native Steam (~/.steam, ~/.local/share/Steam) and Flatpak Steam.
        dirs = ("~/.steam/root/compatibilitytools.d",
                "~/.local/share/Steam/compatibilitytools.d",
                "~/.var/app/com.valvesoftware.Steam/data/Steam/compatibilitytools.d")
        for d in dirs:
            try:
                ge = [v for v in os.listdir(os.path.expanduser(d)) if "GE-Proton" in v]
            except OSError:
                continue
            if ge:
                return "ok", f"GE-Proton installed ({sorted(ge)[-1]}) ✅", ""
        return ("warning", "GE-Proton NOT installed",
                "Download GE-Proton from GitHub → extract to ~/.steam/root/compatibilitytools.d/")
    checks.append(SystemCheck("GE-Proton", check_geproton))

    # === CPU ===

    # The idle CPU governor is a power-efficiency setting, not a gaming one, and
    # on EPP drivers the desktop's power-profiles-daemon owns it — so it lives
    # under the separate Power Saving toggle, not here as a gaming warning.

    # CPU Boost / Turbo — vendor-neutral. AMD (acpi-cpufreq / amd-pstate)
    #     exposes cpufreq/boost; intel_pstate uses no_turbo (inverted). Turbo off
    #     costs a big chunk of single-core clock, which is what games care about.
    def check_cpu_boost():
        boost = _read1("/sys/devices/system/cpu/cpufreq/boost")
        if boost is not None:
            if boost == "1":
                return "ok", "CPU Boost enabled ✅", ""
            return ("warning", "CPU Boost disabled — losing single-core clocks",
                    "Enable it: echo 1 | sudo tee /sys/devices/system/cpu/cpufreq/boost")
        no_turbo = _read1("/sys/devices/system/cpu/intel_pstate/no_turbo")
        if no_turbo is not None:
            if no_turbo == "0":
                return "ok", "Intel Turbo Boost enabled ✅", ""
            return ("warning", "Intel Turbo Boost disabled — losing clocks",
                    "Enable it: echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo")
        return "info", "CPU boost state not exposed by this cpufreq driver", ""
    checks.append(SystemCheck("CPU Boost", check_cpu_boost))

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

        # Game Mode OFF is the normal desktop state, not a problem — parking a
        # CCD permanently would cripple everything but the game. So this is info,
        # not a warning, and it's toggled on the Dashboard, not "fixed" here.
        keep = topo.keep_ccd()
        return ("info",
                f"{count} CCDs — Game Mode available: parks the weaker CCD so CCD{keep} "
                f"({topo.core_count(keep)} cores) gets exclusive cache. Toggle it for gaming.",
                "")
    checks.append(SystemCheck("CCD / Game Mode", check_ccd, info_only=True))

    # === GPU ===

    # 6. NVIDIA driver — honour the exit code. nvidia-smi exits 18 on a
    #    driver/library mismatch and prints the error to *stdout*, so the old
    #    "non-empty stdout = OK" test reported a broken driver as a green tick.
    def check_nvidia():
        if not have_nvidia:
            return "info", "nvidia-smi not found — no NVIDIA GPU or driver not installed", ""
        if _nvml_broken(nv_rc, nv_out):
            if "mismatch" in nv_out.lower():
                return ("critical",
                        "NVIDIA driver/library version mismatch — a newer driver is "
                        "installed but the old module is still loaded",
                        "Reboot to load the matching NVIDIA kernel module")
            first = nv_out.splitlines()[0] if nv_out else f"nvidia-smi exit {nv_rc}"
            return "warning", f"nvidia-smi error: {first}", ""
        return "ok", f"NVIDIA driver {nv_out} ✅", ""
    checks.append(SystemCheck("NVIDIA Driver", check_nvidia))

    # 7. NVIDIA P-State (P8 idle bug)
    def check_pstate():
        if not have_nvidia:
            return "info", "No NVIDIA GPU — P-State check skipped", ""
        if not nvml_ok:
            return "info", "GPU P-State unavailable — see the NVIDIA Driver check above", ""
        rc, out = _nvidia_smi(["--query-gpu=pstate,clocks.gr", "--format=csv,noheader"])
        parts = out.split(", ")
        if len(parts) >= 2:
            pstate, clock = parts[0], parts[1]
            if pstate == "P8" and "555" in clock:
                return "warning", f"GPU stuck at {pstate} ({clock}) — known NVIDIA bug", "nvidia-smi -lgc 1815,1815 (fix GPU clock)"
            return "ok", f"GPU P-State: {pstate} ({clock}) ✅", ""
        return "info", "Could not read GPU P-State", ""
    checks.append(SystemCheck("GPU P-State", check_pstate))

    # 8. ReBAR
    def check_rebar():
        if not have_nvidia:
            return "info", "No NVIDIA GPU — ReBAR check skipped", ""
        if not nvml_ok:
            return "info", "ReBAR unavailable — see the NVIDIA Driver check above", ""
        rc, out = _nvidia_smi(["-q", "-d", "MEMORY"])
        m = re.search(r'BAR1 Memory Usage.*?Total\s*:\s*(\d+)\s*MiB', out, re.DOTALL)
        if m:
            bar = int(m.group(1))
            if bar <= 256:
                return "info", f"ReBAR: {bar}MB (hardware limit on RTX 20-series — no VBIOS fix exists)", ""
            return "ok", f"ReBAR: {bar}MB ✅", ""
        return "info", "ReBAR status unclear", "Enable 'Above 4G Decoding' + 'Re-Size BAR Support' in BIOS"
    checks.append(SystemCheck("ReBAR", check_rebar, info_only=True))

    # 9. Coolbits (OC support). Coolbits is an Xorg-only mechanism: the option
    #    lives in xorg.conf and nvidia-settings can only apply clock offsets when
    #    a real X screen is present. On Wayland the fix would write a file the
    #    session never reads and the offset OC never works — so we don't offer it
    #    there and point at the Wayland-safe path (clock locking) instead.
    def check_coolbits():
        if not have_nvidia:
            return "info", "No NVIDIA GPU — Coolbits check skipped", ""
        if os.environ.get("WAYLAND_DISPLAY"):
            return ("info",
                    "GPU offset overclocking via Coolbits needs an X11 session — "
                    "not possible on Wayland",
                    "On Wayland use clock locking (Benchmark tab); switch to X11 for offset OC")
        if not nvml_ok:
            return "info", "Coolbits unavailable — see the NVIDIA Driver check above", ""
        try:
            r = subprocess.run(["nvidia-settings", "-q", "GPUGraphicsClockOffsetAllPerformanceLevels"],
                              capture_output=True, text=True, timeout=3)
            if "range" in r.stdout.lower() or ": 0" in r.stdout:
                return "ok", "Coolbits enabled — GPU overclocking available ✅", ""
            return "warning", "Coolbits NOT enabled", "nvidia-xconfig --cool-bits=28 or edit Xorg config"
        except Exception:
            return "info", "Could not check Coolbits", ""
    checks.append(SystemCheck("Coolbits / GPU-OC", check_coolbits))

    # 9b. AMD GPU — only added when an amdgpu card is actually bound, so NVIDIA
    #     and Intel machines never see it. Kept at info level: this project has no
    #     AMD hardware to verify deeper probes (SAM state, RADV, clock domains)
    #     against, and a green tick we can't stand behind would be dishonest.
    if have_amd_gpu:
        def check_amd_gpu():
            return ("info",
                    "AMD GPU (amdgpu) in use — Mesa/RADV drives Vulkan",
                    "Enable Re-Size BAR / Smart Access Memory in BIOS for best performance")
        checks.append(SystemCheck("AMD GPU", check_amd_gpu, info_only=True))

    # === System ===

    # 10. NVreg_PreserveVideoMemoryAllocations
    def check_nvreg():
        if not have_nvidia:
            return "info", "No NVIDIA GPU — modprobe tweak not applicable", ""
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
            return "info", f"X11 active ({x11})", "Wayland is recommended for gaming"
        return "info", "Session type unclear", ""
    checks.append(SystemCheck("Display Session", check_session))

    # 12. gamemode.ini — the CCD-parking config only makes sense on a 2+ CCD
    #     Ryzen. On Intel or a single-CCD AMD there is nothing to park, so a
    #     missing file is not a problem — don't nag those machines.
    def check_gamemode_ini():
        multi_ccd = CPUTopology().ccd_count() >= 2
        path = os.path.expanduser("~/.config/gamemode.ini")
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if "park_cores" in content:
                return "ok", "gamemode.ini with CCD config present ✅", ""
            if not multi_ccd:
                return "ok", "gamemode.ini present (core parking not needed on this CPU) ✅", ""
            return "info", "gamemode.ini exists but without CCD config", "Add park_cores + pin_cores for automatic CCD parking when gaming"
        if not multi_ccd:
            return "info", "No gamemode.ini — core parking not applicable to this CPU", ""
        return "warning", "gamemode.ini missing", "Create with park_cores + pin_cores for automatic CCD parking when gaming"
    checks.append(SystemCheck("GameMode Config", check_gamemode_ini))

    # SATA link power is a power-efficiency setting, not a gaming one — it lives
    # under the Power Saving toggle, not here.

    # Audio codec power-save — gaming-relevant, but INVERTED: power_save=1 lets
    # the HDA codec sleep when idle and wake with an audible pop/crackle at the
    # start of a game sound. For a gaming desktop the right value is OFF (0);
    # saving that bit of power belongs to the Power Saving toggle.
    def check_audio():
        val = _read1("/sys/module/snd_hda_intel/parameters/power_save")
        if val is None:
            return "info", "Audio codec power-save not exposed by this driver", ""
        if val == "0":
            return "ok", "Audio codec power-save off ✅ (no wake-up pops in games)", ""
        return ("warning", f"Audio codec power-save on ({val}) — can cause pops/crackle in games",
                "Turn it off so the codec never sleeps mid-game")
    checks.append(SystemCheck("Audio Power Save", check_audio))

    # 15. Monitor refresh rate
    def check_refresh():
        # Use whichever randr tool the session provides: cosmic-randr (COSMIC),
        # wlr-randr (wlroots) or xrandr (X11). Parse the highest refresh the
        # panel offers — enough to spot a high-refresh monitor.
        for tool in ("cosmic-randr", "wlr-randr", "xrandr"):
            if not shutil.which(tool):
                continue
            try:
                args = [tool, "list"] if tool == "cosmic-randr" else [tool]
                out = subprocess.run(args, capture_output=True, text=True, timeout=3).stdout or ""
            except Exception:
                continue
            hz = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*Hz", out)]        # cosmic/wlr
            hz += [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\*", out)]          # xrandr active mode
            if hz:
                top = round(max(hz))
                if top >= 100:
                    return "ok", f"High-refresh monitor detected ({top} Hz) ✅", ""
                return "info", f"Monitor refresh {top} Hz (60 Hz class)", ""
        return "info", "Could not read monitor refresh in this session", ""
    checks.append(SystemCheck("Monitor", check_refresh))

    # === Proton / Vulkan ===

    # 16. Vulkan driver present — no ICD means no modern game runs at all.
    def check_vulkan():
        icd_dir = "/usr/share/vulkan/icd.d"
        try:
            icds = [f for f in os.listdir(icd_dir) if f.endswith(".json")]
        except OSError:
            icds = []
        if icds:
            return "ok", f"Vulkan driver present ({len(icds)} ICD) ✅", ""
        return ("warning", "No Vulkan ICD found — games won't launch",
                "Install your GPU's Vulkan driver (nvidia-utils / vulkan-radeon / vulkan-intel)")
    checks.append(SystemCheck("Vulkan", check_vulkan, info_only=True))

    # 17. vm.max_map_count — the default (65530) is too low for several modern
    #     titles (Star Citizen, Hogwarts Legacy, some DX12 games) which crash on
    #     launch. Distros like CachyOS already raise it.
    def check_max_map_count():
        try:
            with open("/proc/sys/vm/max_map_count") as f:
                val = int(f.read().strip())
        except (OSError, ValueError):
            return "info", "Could not read vm.max_map_count", ""
        if val >= 1048576:
            return "ok", f"vm.max_map_count = {val} ✅", ""
        return ("warning", f"vm.max_map_count = {val} (default is too low for some games)",
                "Raise to 2147483642 so titles like Star Citizen / Hogwarts Legacy don't crash")
    checks.append(SystemCheck("vm.max_map_count", check_max_map_count))

    # 18. Open-file limit — Proton's esync/fsync needs a high hard nofile limit
    #     or games stutter / fail to start. Modern systemd defaults are fine;
    #     older or hand-tuned setups may not be.
    def check_nofile():
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except (ValueError, OSError):
            return "info", "Could not read open-file limit", ""
        if hard >= 524288:
            return "ok", f"Open-file limit (hard) = {hard} ✅ (esync/fsync ready)", ""
        return ("warning", f"Open-file limit (hard) = {hard} — esync needs ≥ 524288",
                "Raise LimitNOFILE (systemd) or nofile in /etc/security/limits.conf, then re-login")
    checks.append(SystemCheck("Open Files (esync)", check_nofile))

    # 19. NVIDIA DRM modeset — required for a working Wayland session on NVIDIA.
    #     The sysfs parameter is root-only, so infer honestly: if a Wayland
    #     session is actually running on NVIDIA, modeset is on by definition.
    def check_modeset():
        if not have_nvidia:
            return "info", "No NVIDIA GPU — modeset check skipped", ""
        try:
            with open("/proc/cmdline") as f:
                cmdline = f.read()
        except OSError:
            cmdline = ""
        on_cmdline = "nvidia_drm.modeset=1" in cmdline or "nvidia-drm.modeset=1" in cmdline
        if os.environ.get("WAYLAND_DISPLAY"):
            return "ok", "NVIDIA DRM modeset active (Wayland session running) ✅", ""
        if on_cmdline:
            return "ok", "NVIDIA DRM modeset=1 set on kernel cmdline ✅", ""
        return ("info", "NVIDIA DRM modeset not confirmed",
                "Set nvidia_drm.modeset=1 for a proper Wayland session")
    checks.append(SystemCheck("NVIDIA Modeset", check_modeset, info_only=True))

    return checks


if __name__ == "__main__":
    print("=== Gaming Command Center — System Scan ===\n")
    checks = scan_system()
    crit = sum(1 for c in checks if c.status == "critical")
    ok = sum(1 for c in checks if c.status == "ok")
    warn = sum(1 for c in checks if c.status == "warning")
    info = sum(1 for c in checks if c.status == "info")
    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    for c in sorted(checks, key=lambda c: order.get(c.status, 4)):
        icon = {"critical": "🛑", "ok": "✅", "warning": "⚠️", "info": "ℹ️"}.get(c.status, "•")
        print(f"{icon} {c.name}: {c.message}")
        if c.fix_message and c.status in ("warning", "critical"):
            print(f"   → Fix: {c.fix_message}")
    print(f"\n=== Summary: {crit} Critical, {ok} OK, {warn} Warnings, {info} Info ===")