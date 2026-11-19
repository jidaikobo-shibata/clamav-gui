"""Start the scanner only after the owner attached this helper to its job."""
import subprocess
import sys

if __name__ == '__main__':
    if sys.stdin.buffer.read(1) != b'1':
        sys.exit(2)
    sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
