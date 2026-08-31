#!/usr/bin/env python3
"""Run a command with its stdin on a socket, as Claude Code does for hooks.

A pipe hides a whole class of bug here: `$(</dev/stdin)` reads a pipe happily
and fails with ENXIO on a socket. usage: socket-stdin.py <payload-file> <cmd>...
"""

#  This file is part of adversarial-review-loop.
#
#  Copyright (c) 2026 Roberto Leinardi
#
#  adversarial-review-loop is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  adversarial-review-loop is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

import socket
import subprocess
import sys
from pathlib import Path

payload = Path(sys.argv[1]).read_bytes()
parent, child = socket.socketpair()
parent.sendall(payload)
parent.shutdown(socket.SHUT_WR)
proc = subprocess.run(sys.argv[2:], check=False, stdin=child.fileno(), capture_output=True)
sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
sys.exit(proc.returncode)
