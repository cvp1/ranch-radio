#!/usr/bin/env python3
"""A fake `ssh` for the HaLow add-on's tests: behaves like an OpenWrt HaLow
bridge answering one command. The command is the last argument (as ssh gets
it). FAKE_HALOW_DIR holds:

  read.txt     what the read script prints (the test writes it)
  down         if present: the bridge doesn't answer (exit 255)
  cmds.log     every command run, one JSON string per line (appended)
  mesh-<host>.txt  what an OpenMANET node of that name prints for the mesh
               read (halow_mesh.MESH_READ); a node with no file doesn't answer
  mesh.log     each mesh read: {"target": user@host, "proxy": the ProxyCommand or null}
"""
import json
import os
import sys
import time

d = os.environ["FAKE_HALOW_DIR"]
cmd = sys.argv[-1]
with open(os.path.join(d, "cmds.log"), "a") as f:
    f.write(json.dumps(cmd) + "\n")
if cmd.startswith("echo @@board"):
    target = sys.argv[-2]
    proxy = next((a[len("ProxyCommand="):] for a in sys.argv if a.startswith("ProxyCommand=")), None)
    with open(os.path.join(d, "mesh.log"), "a") as f:
        f.write(json.dumps({"target": target, "proxy": proxy}) + "\n")
    path = os.path.join(d, f"mesh-{target.split('@')[-1]}.txt")
    if not os.path.exists(path):
        print(f"ssh: connect to host {target.split('@')[-1]} port 22: No route to host", file=sys.stderr)
        sys.exit(255)
    with open(path) as f:
        sys.stdout.write(f.read())
    sys.exit(0)
if os.path.exists(os.path.join(d, "down")):
    print("ssh: connect to host 192.168.1.65 port 22: Connection refused", file=sys.stderr)
    sys.exit(255)
if cmd.startswith("echo @@uptime"):
    # As real ssh does: a warning on stderr first, and the output arriving in
    # more than one chunk (a reader that takes only the first chunk sees half).
    print("** WARNING: connection is not using a post-quantum key exchange algorithm.", file=sys.stderr, flush=True)
    with open(os.path.join(d, "read.txt")) as f:
        text = f.read()
    half = len(text) // 2
    sys.stdout.write(text[:half])
    sys.stdout.flush()
    time.sleep(0.15)
    sys.stdout.write(text[half:])
    sys.exit(0)
if cmd.startswith("ping "):
    print("3 packets transmitted, 3 packets received, 0% packet loss")
    sys.exit(0)
if cmd.startswith("iw dev ") and " station del " in cmd:
    sys.exit(0)
if cmd.startswith("uci set ") or cmd.startswith("/etc/init.d/") or cmd == "reboot":
    sys.exit(0)
print(f"sh: unexpected: {cmd}", file=sys.stderr)
sys.exit(127)
