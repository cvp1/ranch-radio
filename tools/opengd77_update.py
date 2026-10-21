#!/usr/bin/env python3
"""Update OpenGD77 on the MD-UV390 without Windows: the checks around the flash.

The flash itself is OpenGD77's own STM32 loader (opengd77_stm32_firmware_loader.py,
F1RMB / VK3KYY, shipped in the OpenGD77 source releases): Python + pyusb, the
same DFU protocol and donor patch as the Windows CPS's Firmware Loader. It
erases and writes the STM32's internal flash from 0x0800C000 up; the
bootloader below that is never touched, so a failed flash can be redone, and
the SPI flash (calibration, codeplug) is not erased.

    tools/opengd77_update.py backup    a full raw backup: calibration, EEPROM, SPI flash, MCU ROM,
                                       each read twice and compared (MD-UV390 add-on switched off)
    tools/opengd77_update.py check     before: files, hashes, backup, which radio, DFU mode
    tools/opengd77_update.py verify    after: new firmware, calibration and codeplug vs the backup

Paths (mac-shack, 2026-10-17): ~/.local/share/opengd77/{opengd77_stm32_firmware_loader.py,
donor/, firmware/R20260131/}; venv ~/.venvs/opengd77 (pyusb); backups under
~/.local/state/radio-remote/opengd77/raw-backup-*.
"""

import glob
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import opengd77 as g  # noqa: E402

SHARE = os.path.expanduser("~/.local/share/opengd77")
LOADER = os.path.join(SHARE, "opengd77_stm32_firmware_loader.py")
DONOR = os.path.join(SHARE, "donor", "MD9600-CSV(2571V5)-V26.45.bin")
FIRMWARE = os.path.join(SHARE, "firmware", "R20260131", "OpenMDUV380_10W_PLUS.bin")
VENV_PY = os.path.expanduser("~/.venvs/opengd77/bin/python")
BACKUPS = os.path.expanduser("~/.local/state/radio-remote/opengd77/raw-backup-*")
INI = os.path.expanduser("~/.gd77firmwareloader.ini")

PINNED = {
    LOADER: "6b43764542a146195c089e46cfb3c7524250c68243290078d89a5f72823e8367",
    DONOR: "d8a653307222e576ee416ab6d4704d14758fa71c1b0e826ffd840f26266cf11f",   # the one the loader accepts
    FIRMWARE: "a7a0f01841a5f0145f566c440a81aa1427aa047b7ef3e1911435a78ce8199997",  # R20260131 10W PLUS
}
DFU_ID = (0x0483, 0xDF11)
PLUS_PRODUCT = "OpenMDUV380Plus 10W"     # the running radio's USB product name: the 10 W PLUS build is the right one

fails = 0


def say(ok, what, detail=""):
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {what}" + (f": {detail}" if detail else ""))


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def usb_products():
    out = subprocess.run(["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True, text=True).stdout
    names = [line.split("=", 1)[1].strip().strip('"') for line in out.splitlines() if '"USB Product Name"' in line]
    ids = [(int(v), int(p)) for v, p in zip(
        [line.split("=")[1] for line in out.splitlines() if '"idVendor"' in line],
        [line.split("=")[1] for line in out.splitlines() if '"idProduct"' in line])]
    return names, ids


FULL_BACKUP = ("calibration.bin", "eeprom-64k.bin", "spiflash-16m.bin", "mcurom-1m.bin")


def latest_backup():
    """The newest full raw backup (all four images). Partial backups, such as
    the codeplug-area copy taken before a codeplug write, don't count."""
    found = [b for b in sorted(glob.glob(BACKUPS)) if all(os.path.exists(os.path.join(b, n)) for n in FULL_BACKUP)]
    return found[-1] if found else None


LEGACY_FLAGS = 0x81    # channel byte 51 (flag4): 0x80 Power, 0x01 Squelch


def legacy_flags(r):
    """Channels carrying the official firmware's power (0x80) and squelch
    (0x01) bits in flag4. OpenGD77 R20260131 never reads them (power is
    libreDMR_Power, squelch is the sql byte; codeplug.c flagAccessor has
    CHANNEL_FLAG_POWER and CHANNEL_FLAG_SQUELCH and nothing calls them).
    They were left in 12 of the MD-UV390's slots by channels deleted long
    ago (mac-shack, 2026-10-17). qdmr can't clear them; the OpenGD77 CPS can."""
    out = []
    for n in r.channel_slots():
        area, addr = g.slot_addr(n)
        b = r.read(area, addr, g.CHANNEL_SIZE)
        if b[51] & LEGACY_FLAGS:
            out.append((n, g.channel_name(b), b[51] & LEGACY_FLAGS))
    return out


def report_legacy_flags(r):
    found = legacy_flags(r)
    if not found:
        say(True, "no channel carries the legacy flag4 power/squelch bits")
        return
    names = ", ".join(f"{n} {name}" for n, name, _ in found)
    print(f"note  {len(found)} channels carry the legacy flag4 power/squelch bits (0x80, 0x01): {names}.\n"
          "      R20260131 ignores them. Before flashing another release, search its source for\n"
          "      CHANNEL_FLAG_POWER and CHANNEL_FLAG_SQUELCH (application/source); if anything reads\n"
          "      them, clear the bits in the OpenGD77 CPS first.")


CODEPLUGS = os.path.expanduser("~/.local/state/radio-remote/opengd77")
IMAGES = (("calibration.bin", 1, 0x10000, 0x200),      # area 1 = SPI flash; the calibration copy
          ("eeprom-64k.bin", 2, 0, 0x10000),
          ("spiflash-16m.bin", 1, 0, 0x1000000),
          ("mcurom-1m.bin", 5, 0, 0x100000))           # area 5 = STM32 flash from 0x08000000


def backup():
    """Read each image twice; keep it only if both reads agree."""
    import time
    out = os.path.join(CODEPLUGS, time.strftime("raw-backup-%Y-%m-%d_%H%M%S"))
    r = g.OpenGD77("auto", timeout=3.0)
    r.open()
    sums = []
    try:
        for name, area, start, size in IMAGES:
            a = read_range(r, area, start, size)
            b = read_range(r, area, start, size)
            say(a == b, f"{name}: two reads agree", f"{size} bytes")
            if a != b:
                return 1
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, name), "wb") as f:
                f.write(a)
            sums.append(f"{hashlib.sha256(a).hexdigest()}  {name}\n")
    finally:
        r.close()
    with open(os.path.join(out, "SHA256SUMS"), "w") as f:
        f.writelines(sums)
    print(f"      {out}")
    return 1 if fails else 0


def backup_is_current(b):
    """The backup is newer than the last codeplug written to the radio."""
    writes = glob.glob(os.path.join(CODEPLUGS, "*-read-back.yaml"))
    last = max((os.path.getmtime(w) for w in writes), default=0)
    return b is not None and os.path.getmtime(os.path.join(b, "SHA256SUMS")) > last


def check():
    for path, want in PINNED.items():
        exists = os.path.exists(path)
        say(exists and sha(path) == want, f"{os.path.basename(path)} present, sha256 pinned", "" if exists else "missing")
    say(os.path.exists(VENV_PY), "loader venv (pyusb)", VENV_PY)
    reg = os.path.exists(INI) and DONOR in open(INI).read()
    say(reg, "donor registered with the loader (DMR, not FM only)",
        "" if reg else f"run once: {VENV_PY} {LOADER} -s '{DONOR}'")
    b = latest_backup()
    good = False
    if b and os.path.exists(os.path.join(b, "SHA256SUMS")):
        rows = [line.split() for line in open(os.path.join(b, "SHA256SUMS")) if line.strip()]
        good = len(rows) >= 4 and all(sha(os.path.join(b, n)) == h for h, n in rows)
    say(good, "raw backup intact (calibration, EEPROM, SPI flash, MCU ROM)", b or "none")
    say(good and backup_is_current(b), "backup taken after the last codeplug write",
        "" if good and backup_is_current(b) else "run: tools/opengd77_update.py backup (MD-UV390 add-on off)")
    names, ids = usb_products()
    if DFU_ID in ids:
        say(True, "radio is in update (DFU) mode, 0483:DF11")
        print(f"\nReady. Flash with:\n\n  {VENV_PY} {LOADER} -m MD-UV380 -f {FIRMWARE}\n\n"
              "It must print \"Patching for DMR\". Then switch the radio off and on, plug it in normally, "
              "and run: tools/opengd77_update.py verify")
    elif PLUS_PRODUCT in " ".join(names):
        say(True, "the running radio is the 10 W Plus, so the 10W_PLUS build is the right one")
        try:
            r = g.OpenGD77("auto", timeout=3.0)
            r.open()
            try:
                report_legacy_flags(r)
            finally:
                r.close()
        except (OSError, g.OpenGD77Error) as e:
            print(f"note  channels not checked for legacy flag bits ({e}); switch the MD-UV390 add-on off and run check again")
        print("\nNot in update mode yet: switch the radio off, hold S1 (top side button) and PTT, switch it on. "
              "The screen stays blank. Then run check again.")
    else:
        say(False, "radio not found (neither running OpenGD77 nor in update mode)")
    return 1 if fails else 0


def read_range(r, area, start, size):
    buf = bytearray()
    for a in range(start, start + size, 1024):
        buf += r.read(area, a, min(1024, start + size - a))
    return bytes(buf)


def verify():
    b = latest_backup()
    if not b:
        say(False, "no backup to compare with")
        return 1
    r = g.OpenGD77("auto", timeout=3.0)
    r.open()
    try:
        info = r.info()
        say(info["type"] == 6, "radio answers as an MD-UV380/390", f"firmware {info['revision']} built {info['built']}")
        say(info["built"].startswith("2026"), "new firmware (built 2026)", info["built"])
        cal = read_range(r, 1, 0x10000, 0x200)
        say(cal == open(os.path.join(b, "calibration.bin"), "rb").read(), "calibration unchanged")
        old = open(os.path.join(b, "eeprom-64k.bin"), "rb").read()
        new = read_range(r, 2, 0, 0x10000)
        chan = slice(0x3780, 0x3790 + 128 * 56)                  # bank 0: in-use bitmap + 128 channels
        say(new[chan] == old[chan], "channels 1-128 unchanged")
        zones = slice(0x8000, 0x8030 + 68 * (16 + 2 * 80))       # zone header + list (codeplug.c)
        say(new[zones] == old[zones], "zones unchanged")
        diff = sum(1 for x, y in zip(old, new) if x != y)
        print(f"      EEPROM bytes changed overall: {diff} (settings may move between releases)")
        print(f"      channel slots: {r.channel_slots()}")
        report_legacy_flags(r)
    finally:
        r.close()
    return 1 if fails else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("backup", "check", "verify"):
        sys.exit(__doc__)
    sys.exit({"backup": backup, "check": check, "verify": verify}[cmd]())
