"""Job wrapper: python -m vaniloop.console_log <logfile> <exitfile> -- <argv...>

The console starts every job through this wrapper, in its own process group. The wrapper runs the real
command in the same group, copies its output to the log with secrets masked, and writes the exit code to
<exitfile>. Because the wrapper owns the output pipe, a console restart cannot break the job's stdout.
"""

from __future__ import annotations

import os
import subprocess
import sys

from vaniloop.console import mask


def main(argv: list[str]) -> int:
    log_path, exit_path, cmd = argv[0], argv[1], argv[argv.index("--") + 1 :]
    with open(log_path, "ab") as log:
        try:
            child = subprocess.Popen(  # noqa: S603 - argv list built by the console, never a shell
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
        except OSError as e:
            log.write(mask(f"cannot start job: {e.strerror}").encode() + b"\n")
            code = 127
        else:
            for raw in child.stdout:
                log.write(mask(raw.decode("utf-8", errors="replace").rstrip("\r\n")).encode("utf-8") + b"\n")
                log.flush()
            code = child.wait()
    tmp = exit_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(code))
    os.replace(tmp, exit_path)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
