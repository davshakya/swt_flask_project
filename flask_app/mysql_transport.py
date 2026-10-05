"""Local transport recovery for shared-host MySQL connection resets."""
import os
from pathlib import Path
import stat


def discover_local_mysql_socket(config, environ=None):
    environ = os.environ if environ is None else environ
    # Do not redirect remote databases, nonstandard instances, or TLS policy.
    if (os.name != "posix" or config.get("host") != "localhost"
            or int(config.get("port", 3306)) != 3306
            or environ.get("MYSQL_SSL_CA", "").strip()):
        return None
    for candidate in ("/var/lib/mysql/mysql.sock", "/run/mysqld/mysqld.sock",
                      "/var/run/mysqld/mysqld.sock", "/tmp/mysql.sock"):
        try:
            if stat.S_ISSOCK(Path(candidate).stat().st_mode):
                return candidate
        except OSError:
            continue
    return None
