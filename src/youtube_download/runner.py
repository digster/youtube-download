"""Run yt-dlp in a process group that cannot outlive its owning application."""

import os
import fcntl
import signal
import sys
import threading
import time


def main():
    parent_pid = int(sys.argv[1])

    def watch_parent():
        # A hard-killed server cannot run shutdown handlers. Stop this process and
        # ffmpeg children before a restarted server resumes their partial files.
        while True:
            if os.getppid() != parent_pid:
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return
            time.sleep(0.25)

    threading.Thread(target=watch_parent, daemon=True).start()
    import yt_dlp

    # A per-video lock also protects a moved folder and prevents a restarted
    # server from touching partial files before its predecessor has exited.
    with open(sys.argv[2], "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yt_dlp.main(sys.argv[3:])


if __name__ == "__main__":
    main()
