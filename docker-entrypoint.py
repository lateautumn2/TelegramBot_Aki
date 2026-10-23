"""Container entrypoint.

The data directory is usually a bind mount that the host created as root.
Starting as root, hand it to the unprivileged `app` user, drop privileges,
then exec the real command (so it becomes PID 1 and receives SIGTERM).
"""

import os
import pwd
import sys


def main() -> None:
    if os.getuid() == 0:
        user = pwd.getpwnam("app")
        data_dir = os.environ.get("DATA_DIR", "/data")
        os.makedirs(data_dir, exist_ok=True)
        for root, _dirs, files in os.walk(data_dir):
            for path in [root, *(os.path.join(root, f) for f in files)]:
                os.chown(path, user.pw_uid, user.pw_gid)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        os.environ["HOME"] = user.pw_dir
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
