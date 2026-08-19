#!/usr/bin/env python3
"""Run a command with its stdin on a socket, as Claude Code does for hooks.

A pipe hides a whole class of bug here: `$(</dev/stdin)` reads a pipe happily
and fails with ENXIO on a socket. usage: socket-stdin.py <payload-file> <cmd>...
"""
import socket
import subprocess
import sys

payload = open(sys.argv[1], "rb").read()
parent, child = socket.socketpair()
parent.sendall(payload)
parent.shutdown(socket.SHUT_WR)
proc = subprocess.run(sys.argv[2:], stdin=child.fileno(), capture_output=True)
sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
sys.exit(proc.returncode)
