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
"""CPU topology detection for Gaming Command Center.

Single source of truth for the CCD layout. The GUI and the system scanner both
import from here so that no CPU number is ever hardcoded.

Offline CPUs disappear from sysfs entirely — their cache/ and topology/ dirs are
removed by the kernel — so a complete detection is cached on disk and reused
while cores are parked.
"""
import json
import os
import re
import subprocess

SYS_CPU = "/sys/devices/system/cpu"
CONFIG_DIR = os.path.expanduser("~/.config/gaming-command-center")
TOPO_CACHE = os.path.join(CONFIG_DIR, "topology.json")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def parse_cpu_list(s):
    """'0-5,12,14-15' -> [0,1,2,3,4,5,12,14,15]"""
    cpus = []
    if not s:
        return cpus
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def format_cpu_list(cpus):
    """[6,7,8,9,18,19] -> '6-9,18-19' (the format gamemode.ini expects)"""
    cpus = sorted(set(cpus))
    if not cpus:
        return ""
    ranges = []
    start = prev = cpus[0]
    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append((start, prev))
        start = prev = cpu
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


# ============================================================
# User config (which CCD to keep when Game Mode is on)
# ============================================================
def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(updates):
    cfg = load_config()
    cfg.update(updates)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except OSError:
        return False


class CPUTopology:
    """CCD layout + live CPU state, detected from sysfs."""

    def __init__(self):
        self.ccds = {}        # ccd_id -> [cpu, ...]  (every thread, incl. offline)
        self.ccx = {}         # ccx_id -> [cpu, ...]  (L3 groups; 2 per CCD on Zen 2)
        self.siblings = {}    # cpu -> [SMT sibling cpus]
        self.complete = False  # is the full layout known?
        self.source = "none"   # "sysfs" | "cache" | "partial"
        self.detect()

    # ---------- detection ----------

    def present_cpus(self):
        """Every CPU the kernel knows about, online or not."""
        return parse_cpu_list(_read(f"{SYS_CPU}/present")) or [0]

    def offline_cpus(self):
        return parse_cpu_list(_read(f"{SYS_CPU}/offline"))

    def detect(self):
        present = self.present_cpus()
        offline = set(self.offline_cpus())

        ccx = {}       # L3 cache id -> [cpu, ...]
        core_ids = {}  # cpu -> topology core_id
        siblings = {}
        for cpu in present:
            if cpu in offline:
                continue
            l3 = _read(f"{SYS_CPU}/cpu{cpu}/cache/index3/id")
            if l3 is None:
                continue
            ccx.setdefault(int(l3), []).append(cpu)
            core = _read(f"{SYS_CPU}/cpu{cpu}/topology/core_id")
            if core is not None:
                core_ids[cpu] = int(core)
            sibs = parse_cpu_list(
                _read(f"{SYS_CPU}/cpu{cpu}/topology/thread_siblings_list"))
            siblings[cpu] = sibs or [cpu]

        if ccx and not offline:
            # Nothing is parked — this reading is authoritative.
            self._set_groups(ccx, core_ids, siblings)
            self.complete = True
            self.source = "sysfs"
            self._save_cache()
            return

        if self._load_cache(present):
            return

        # Cores are parked and we have never seen the machine intact. We only
        # know the online half of the layout — the GUI must ask the user to
        # unpark before offering Game Mode.
        self._set_groups(ccx, core_ids, siblings)
        self.complete = False
        self.source = "partial"

    def _set_groups(self, ccx, core_ids, siblings):
        """Turn L3 cache groups into CCDs.

        One L3 instance is one CCX. On Zen 3+ a CCD has exactly one CCX, so L3
        groups already are CCDs. On Zen 2 a CCD holds *two* CCXs (a 3900X has
        4 L3 groups but only 2 CCDs), and the kernel exposes no die info for it:
        die_id is 0 for every core and cluster_id is unset.

        The APIC ID does encode it though. AMD reserves 8 core_id slots per CCD,
        so core_id // 8 is the die index — on a 3900X that merges the CCX pairs
        (core_id 0-6 and 8-14) into CCD0 and CCD1.

        This only holds while a CCD tops out at 8 cores (every Zen desktop part
        so far). If an L3 group ever straddles a die boundary the rule broke, so
        we verify that and fall back to plain L3 grouping rather than guessing.
        """
        self.siblings = siblings
        self.ccx = {i: sorted(ccx[key]) for i, key in enumerate(sorted(ccx))}

        dies = self._merge_ccx_into_dies(ccx, core_ids)
        groups = dies if dies else ccx
        self.ccds = {i: sorted(groups[key]) for i, key in enumerate(sorted(groups))}

    def _merge_ccx_into_dies(self, ccx, core_ids):
        """Group CCXs by core_id // 8, or None if that rule does not hold."""
        if self.vendor() != "AuthenticAMD" or not core_ids:
            return None
        dies = {}
        for l3_id, cpus in ccx.items():
            indices = {core_ids[c] // 8 for c in cpus if c in core_ids}
            if len(indices) != 1:
                return None  # an L3 straddles a die boundary — rule is invalid
            dies.setdefault(indices.pop(), []).extend(cpus)
        return dies or None

    def vendor(self):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("vendor_id"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return ""

    def ccx_per_ccd(self):
        """>1 on Zen 2, where a CCD is built from two CCXs."""
        if not self.ccds or not getattr(self, "ccx", None):
            return 1
        return max(1, len(self.ccx) // len(self.ccds))

    def _save_cache(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(TOPO_CACHE, "w") as f:
                json.dump({
                    "cpu": self.get_cpu_name(),
                    "present": self.present_cpus(),
                    "ccds": {str(k): v for k, v in self.ccds.items()},
                    "ccx": {str(k): v for k, v in self.ccx.items()},
                    "siblings": {str(k): v for k, v in self.siblings.items()},
                }, f, indent=2)
        except OSError:
            pass

    def _load_cache(self, present):
        try:
            with open(TOPO_CACHE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        # Distrust the cache if it was written on a different CPU or the set of
        # present CPUs changed (BIOS core count, different machine, same $HOME).
        if data.get("cpu") != self.get_cpu_name():
            return False
        if data.get("present") != present:
            return False
        try:
            self.ccds = {int(k): sorted(v) for k, v in data["ccds"].items()}
            self.ccx = {int(k): sorted(v) for k, v in data.get("ccx", {}).items()}
            self.siblings = {int(k): list(v) for k, v in data["siblings"].items()}
        except (KeyError, TypeError, ValueError):
            return False
        self.complete = True
        self.source = "cache"
        return True

    # ---------- layout ----------

    def get_all_ccd_ids(self):
        return sorted(self.ccds.keys())

    def get_ccd_cpus(self, ccd_id):
        return list(self.ccds.get(ccd_id, []))

    def ccd_count(self):
        return len(self.ccds)

    def get_ccd_of_cpu(self, cpu):
        for ccd_id, cpus in self.ccds.items():
            if cpu in cpus:
                return ccd_id
        return None

    def smt_enabled(self):
        val = _read(f"{SYS_CPU}/smt/control")
        if val is None:
            return any(len(s) > 1 for s in self.siblings.values())
        return val not in ("off", "forceoff")

    def get_physical_cores(self, ccd_id):
        """One CPU per physical core — the lowest-numbered SMT sibling."""
        cpus = self.get_ccd_cpus(ccd_id)
        if not any(c in self.siblings for c in cpus):
            # No sibling info (partial detection): Linux enumerates the first
            # thread of every core before the second, so the first half is one
            # thread per core.
            return cpus[:len(cpus) // 2] if self.smt_enabled() and len(cpus) > 1 else cpus
        cores, seen = [], set()
        for cpu in cpus:
            sibs = tuple(sorted(self.siblings.get(cpu, [cpu])))
            if sibs in seen:
                continue
            seen.add(sibs)
            cores.append(min(sibs))
        return cores

    def core_count(self, ccd_id):
        return len(self.get_physical_cores(ccd_id))

    # ---------- parking ----------

    def is_cpu_online(self, cpu):
        val = _read(f"{SYS_CPU}/cpu{cpu}/online")
        if val is None:
            # No 'online' file means the CPU cannot be hotplugged — it is up.
            return os.path.isdir(f"{SYS_CPU}/cpu{cpu}")
        return val == "1"

    def can_park(self, cpu):
        """CPU0 is never parked: some kernels refuse it and some firmware needs it."""
        return cpu != 0 and os.path.isfile(f"{SYS_CPU}/cpu{cpu}/online")

    def is_ccd_parked(self, ccd_id):
        cpus = [c for c in self.get_ccd_cpus(ccd_id) if self.can_park(c)]
        return bool(cpus) and not any(self.is_cpu_online(c) for c in cpus)

    def get_parked_ccds(self):
        return [c for c in self.get_all_ccd_ids() if self.is_ccd_parked(c)]

    def game_mode_active(self):
        return bool(self.get_parked_ccds())

    def park_plan(self, keep_ccd):
        """CPUs to take offline so that only `keep_ccd` is left running.

        CPU0 is excluded, so keeping a CCD that does not contain CPU0 leaves
        CPU0 (and its SMT sibling) online. That is intentional — see can_park().
        """
        cpus = []
        for ccd_id in self.get_all_ccd_ids():
            if ccd_id != keep_ccd:
                cpus.extend(c for c in self.get_ccd_cpus(ccd_id) if self.can_park(c))
        return sorted(cpus)

    def unpark_plan(self):
        """Every parkable CPU, so Game Mode can be switched off."""
        return sorted(c for c in self.present_cpus() if self.can_park(c))

    def keep_ccd(self):
        """Which CCD survives Game Mode. Priority: a manual override from
        Settings, else the benchmark winner, else the first CCD."""
        ids = self.get_all_ccd_ids()
        if not ids:
            return None
        cfg = load_config()
        manual = cfg.get("keep_ccd_manual")
        if manual in ids:
            return manual
        preferred = cfg.get("keep_ccd")  # benchmark winner
        if preferred in ids:
            return preferred
        return ids[0]

    # ---------- live state ----------

    def online_thread_count(self):
        return sum(1 for c in self.present_cpus() if self.is_cpu_online(c))

    def online_core_count(self):
        threads = self.online_thread_count()
        if self.smt_enabled() and threads > 1:
            return threads // 2
        return threads

    def get_cpu_freq(self, cpu):
        val = _read(f"{SYS_CPU}/cpu{cpu}/cpufreq/scaling_cur_freq")
        try:
            return int(val) // 1000
        except (TypeError, ValueError):
            return 0

    def get_governor(self):
        return _read(f"{SYS_CPU}/cpu0/cpufreq/scaling_governor") or "?"

    def get_temp(self):
        try:
            r = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.split("\n"):
                if "Tctl:" in line:
                    m = re.search(r"([+-]?[\d.]+)°C", line)
                    if m:
                        return float(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
        return 0.0

    def get_cpu_name(self):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "Unknown CPU"


if __name__ == "__main__":
    topo = CPUTopology()
    ccx_note = f", {topo.ccx_per_ccd()} CCX each" if topo.ccx_per_ccd() > 1 else ""
    print(f"CPU:       {topo.get_cpu_name()}")
    print(f"Detection: {topo.source} (complete={topo.complete})")
    print(f"CCDs:      {topo.ccd_count()}{ccx_note}")
    for ccd_id in topo.get_all_ccd_ids():
        cpus = topo.get_ccd_cpus(ccd_id)
        state = "PARKED" if topo.is_ccd_parked(ccd_id) else "active"
        print(f"  CCD{ccd_id}: {topo.core_count(ccd_id)} cores / {len(cpus)} threads "
              f"— CPUs {format_cpu_list(cpus)} [{state}]")
    keep = topo.keep_ccd()
    print(f"\nKeep CCD:  {keep}")
    print(f"Park plan: {format_cpu_list(topo.park_plan(keep)) or '(nothing)'}")
