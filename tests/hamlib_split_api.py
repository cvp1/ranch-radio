"""Split through libhamlib's C API, the way JTDX/WSJT-X call it (not the
rigctl CLI, which takes a different path and once hid a split bug).

    python3 tests/hamlib_split_api.py <rigctl-port>
Exits 0 if every split call succeeds. Needs libhamlib (skips if absent).
"""
import ctypes, ctypes.util, os, sys
lib = next((p for p in (os.environ.get("HAMLIB_LIB"), ctypes.util.find_library("hamlib"),
                        "/opt/homebrew/lib/libhamlib.dylib", "/usr/lib/libhamlib.so.4") if p and os.path.exists(p)), None)
if not lib:
    print("SKIP: libhamlib not found"); sys.exit(0)
hl = ctypes.CDLL(lib)
hl.rig_set_debug(0)
N = lambda n: 1 << n
VFO_A, VFO_B, VFO_CURR = N(0), N(1), N(29)
VFO_TX = VFO_CURR | N(30)
hl.rig_init.restype = ctypes.c_void_p
rig = ctypes.c_void_p(hl.rig_init(2))
hl.rig_token_lookup.restype = ctypes.c_long
tok = hl.rig_token_lookup(rig, b"rig_pathname")
hl.rig_set_conf(rig, ctypes.c_long(tok), b"127.0.0.1:" + sys.argv[1].encode())
opened = hl.rig_open(rig)
if opened != 0:
    print("FAIL: rig_open", opened); sys.exit(1)
bad = 0
hl.rig_set_split_vfo.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
hl.rig_set_split_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_double]
hl.rig_set_split_freq_mode.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_double, ctypes.c_uint64, ctypes.c_long]
for rx in ("CURR", "A"):
    for fv in ("CURR", "TX", "B", "A"):
        hl.rig_set_split_vfo(rig, {"CURR": VFO_CURR, "A": VFO_A}[rx], 1, VFO_B)
        r = hl.rig_set_split_freq(rig, {"CURR": VFO_CURR, "TX": VFO_TX, "B": VFO_B, "A": VFO_A}[fv], ctypes.c_double(14075500))
        bad += r != 0
        print(f"{'ok  ' if r == 0 else 'FAIL'}  split_vfo(rx={rx}) + set_split_freq(vfo={fv}) -> {r}")
r = hl.rig_set_split_freq_mode(rig, VFO_CURR, ctypes.c_double(14075600), ctypes.c_uint64(0x800), ctypes.c_long(0))
bad += r != 0
print(f"{'ok  ' if r == 0 else 'FAIL'}  set_split_freq_mode(CURR, PKTUSB) -> {r}")
# A client may set the TX frequency with rig_set_freq(VFO_B). It must reach the
# split TX side and leave VFO A alone (in non-VFO mode it landed on A).
hl.rig_set_cache_timeout_ms.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
hl.rig_set_cache_timeout_ms(rig, 0, 0)          # HAMLIB_CACHE_ALL: read the server
hl.rig_set_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_double]
hl.rig_get_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_double)]
hl.rig_get_split_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_double)]
a0, a1, tx = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
hl.rig_get_freq(rig, VFO_A, ctypes.byref(a0))
r = hl.rig_set_freq(rig, VFO_B, ctypes.c_double(14074055))
hl.rig_get_freq(rig, VFO_A, ctypes.byref(a1))
hl.rig_get_split_freq(rig, VFO_CURR, ctypes.byref(tx))
good = r == 0 and a1.value == a0.value and int(tx.value) == 14074055
bad += not good
print(f"{'ok  ' if good else 'FAIL'}  set_freq(VFO_B) -> TX {int(tx.value)}, VFO A {int(a0.value)} -> {int(a1.value)}")
hl.rig_close(rig)
print("PASS" if not bad else f"{bad} FAILED"); sys.exit(1 if bad else 0)
