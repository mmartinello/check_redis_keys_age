#!/usr/bin/env python3
"""
check_redis_keys_age.py - Nagios/Icinga2 plugin to check the age of Redis keys.

Connects to a Redis server and finds all keys whose names match a given
regular expression. If the oldest matching key (by idle time) exceeds the
warning or critical threshold, the check exits accordingly.

Key age is measured via the Redis OBJECT IDLETIME command, which returns
the number of seconds since the key was last read or written. This is a
reliable proxy for "time since last update" for keys that are periodically
refreshed by a background process (heartbeats, job timestamps, etc.).

Requirements:
    pip install redis

Usage:
    check_redis_keys_age.py -H <host> -p <pattern> [options]

Exit codes (Nagios/Icinga standard):
    0 - OK
    1 - WARNING
    2 - CRITICAL
    3 - UNKNOWN
"""

import sys
import re
import argparse

# ---------------------------------------------------------------------------
# Attempt to import the redis package; fail gracefully if missing.
# ---------------------------------------------------------------------------
try:
    import redis
    from redis.exceptions import (
        ConnectionError as RedisConnectionError,
        TimeoutError as RedisTimeoutError,
        AuthenticationError as RedisAuthError,
        ResponseError as RedisResponseError,
    )
except ImportError:
    print(
        "UNKNOWN - Required Python package 'redis' is not installed. "
        "Run: pip install redis"
    )
    sys.exit(3)

# ---------------------------------------------------------------------------
# Nagios/Icinga2 standard exit codes.
# ---------------------------------------------------------------------------
STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2
STATE_UNKNOWN = 3

STATE_NAMES = {
    STATE_OK: "OK",
    STATE_WARNING: "WARNING",
    STATE_CRITICAL: "CRITICAL",
    STATE_UNKNOWN: "UNKNOWN",
}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def exit_plugin(state, message, perfdata=None):
    """Print a single-line plugin result and exit with the given state code."""
    line = f"{STATE_NAMES[state]} - {message}"
    if perfdata:
        line += f" | {perfdata}"
    print(line)
    sys.exit(state)


def build_perfdata(matched_keys, oldest_age=None, warning=None, critical=None):
    """
    Build a Nagios performance data string.

    Format per metric: 'label'=value[UOM];[warn];[crit];[min];[max]
    """
    warn_s = str(warning) if warning is not None else ""
    crit_s = str(critical) if critical is not None else ""

    parts = [f"matched_keys={matched_keys};;;0;"]

    if oldest_age is not None:
        parts.append(f"oldest_key_age={oldest_age}s;{warn_s};{crit_s};0;")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def connect_redis(host, port, db, password, timeout):
    """
    Open a Redis connection and verify it with PING.

    Exits with UNKNOWN on any connection or authentication error.
    """
    try:
        client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            decode_responses=False,
        )
        client.ping()
        return client
    except RedisConnectionError as exc:
        exit_plugin(STATE_UNKNOWN, f"Cannot connect to Redis at {host}:{port} - {exc}")
    except RedisTimeoutError:
        exit_plugin(STATE_UNKNOWN, f"Connection to Redis at {host}:{port} timed out")
    except RedisAuthError as exc:
        exit_plugin(STATE_UNKNOWN, f"Redis authentication failed: {exc}")
    except Exception as exc:
        exit_plugin(STATE_UNKNOWN, f"Unexpected error connecting to Redis: {exc}")


def compile_pattern(pattern):
    """
    Compile the user-supplied regex pattern.

    Exits with UNKNOWN if the expression is invalid.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        exit_plugin(STATE_UNKNOWN, f"Invalid regular expression '{pattern}': {exc}")


def scan_matching_keys(client, regex):
    """
    Iterate over all keys in the database using SCAN (non-blocking) and
    return those whose names match *regex*.

    SCAN is preferred over KEYS because it does not block the Redis event
    loop, making it safe to run against production instances.
    """
    matches = []
    cursor = 0

    while True:
        cursor, keys = client.scan(cursor=cursor, count=100)
        for raw_key in keys:
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            if regex.search(key):
                matches.append(key)
        if cursor == 0:
            break

    return matches


def get_idle_time(client, key):
    """
    Return OBJECT IDLETIME for *key* (seconds since last access).

    Tries the API in order of preference to stay compatible across redis-py
    versions (the method was renamed between major releases):
      - redis-py >= 7: client.object("idletime", key)
      - redis-py < 7:  client.object_idletime(key)
      - fallback:      client.execute_command("OBJECT", "IDLETIME", key)

    Returns None if the key no longer exists or if the command is
    unavailable (some managed Redis services disable it).
    """
    # redis-py >= 7
    if hasattr(client, "object") and callable(client.object):
        try:
            return client.object("idletime", key)
        except (RedisResponseError, Exception):
            pass

    # redis-py < 7
    if hasattr(client, "object_idletime"):
        try:
            return client.object_idletime(key)
        except (RedisResponseError, Exception):
            pass

    # universal fallback via raw command
    try:
        return client.execute_command("OBJECT", "IDLETIME", key)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        prog="check_redis_keys_age",
        description="Nagios/Icinga2 plugin that checks the age of Redis keys.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
DESCRIPTION
  The plugin connects to a Redis server, finds all keys whose names match
  a regular expression, and checks whether the oldest matching key (measured
  by OBJECT IDLETIME) exceeds the warning or critical age thresholds.

  If no keys match the pattern the check exits OK — the absence of a key is
  not considered an error by this plugin.

EXIT CODES
  0 (OK)       No matching keys found, or oldest key within both thresholds.
  1 (WARNING)  Oldest matching key idle time >= warning threshold.
  2 (CRITICAL) Oldest matching key idle time >= critical threshold.
  3 (UNKNOWN)  Cannot connect, authentication failure, or other error.

PERFORMANCE DATA
  matched_keys    Number of keys matching the pattern.
  oldest_key_age  Idle time (seconds) of the oldest matching key.

EXAMPLES
  Warn after 5 min, critical after 10 min of inactivity on heartbeat keys:
    %(prog)s -H localhost -p "heartbeat:.*" -w 300 -c 600

  Non-default port and database, check backup job keys:
    %(prog)s -H redis.example.com -P 6380 -D 2 -p "job:backup:.*" -w 3600 -c 7200

  Password-protected Redis:
    %(prog)s -H localhost -p "myapp:status:.*" -a s3cr3t -w 60 -c 120
""",
    )

    req = parser.add_argument_group("required arguments")
    req.add_argument(
        "-H", "--host",
        required=True,
        metavar="HOST",
        help="Redis server hostname or IP address",
    )
    req.add_argument(
        "-p", "--pattern",
        required=True,
        metavar="PATTERN",
        help="Regular expression matched against Redis key names",
    )

    opt = parser.add_argument_group("optional arguments")
    opt.add_argument(
        "-P", "--port",
        type=int,
        default=6379,
        metavar="PORT",
        help="Redis server port (default: 6379)",
    )
    opt.add_argument(
        "-D", "--database",
        type=int,
        default=0,
        metavar="DB",
        help="Redis database number (default: 0)",
    )
    opt.add_argument(
        "-w", "--warning",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Warning threshold: alert if oldest key idle time >= SECONDS",
    )
    opt.add_argument(
        "-c", "--critical",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Critical threshold: alert if oldest key idle time >= SECONDS",
    )
    opt.add_argument(
        "-a", "--password",
        default=None,
        metavar="PASSWORD",
        help="Redis AUTH password",
    )
    opt.add_argument(
        "-t", "--timeout",
        type=int,
        default=10,
        metavar="SECONDS",
        help="Connection/socket timeout in seconds (default: 10)",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Sanity-check: warning must be strictly less than critical when both set.
    if args.warning is not None and args.critical is not None:
        if args.warning >= args.critical:
            exit_plugin(
                STATE_UNKNOWN,
                "Warning threshold must be strictly less than critical threshold",
            )

    # Connect to Redis.
    client = connect_redis(
        host=args.host,
        port=args.port,
        db=args.database,
        password=args.password,
        timeout=args.timeout,
    )

    # Compile regex and scan for matching keys.
    regex = compile_pattern(args.pattern)

    try:
        matching_keys = scan_matching_keys(client, regex)
    except Exception as exc:
        exit_plugin(STATE_UNKNOWN, f"Error scanning Redis keys: {exc}")

    # No keys match — nothing to alert about.
    if not matching_keys:
        perfdata = build_perfdata(matched_keys=0)
        exit_plugin(
            STATE_OK,
            f"No keys matching pattern '{args.pattern}'",
            perfdata=perfdata,
        )

    # Collect idle times for every matched key.
    key_ages = {}
    for key in matching_keys:
        age = get_idle_time(client, key)
        if age is not None:
            key_ages[key] = age

    if not key_ages:
        exit_plugin(
            STATE_UNKNOWN,
            f"Found {len(matching_keys)} matching key(s) but could not "
            "determine their age (OBJECT IDLETIME unavailable)",
        )

    num_keys = len(matching_keys)
    oldest_key = max(key_ages, key=lambda k: key_ages[k])
    oldest_age = key_ages[oldest_key]

    perfdata = build_perfdata(
        matched_keys=num_keys,
        oldest_age=oldest_age,
        warning=args.warning,
        critical=args.critical,
    )

    # Evaluate critical threshold first (higher severity wins).
    if args.critical is not None and oldest_age >= args.critical:
        exit_plugin(
            STATE_CRITICAL,
            f"Key '{oldest_key}' is {oldest_age}s old "
            f"(critical threshold: {args.critical}s), "
            f"{num_keys} matching key(s) found",
            perfdata=perfdata,
        )

    if args.warning is not None and oldest_age >= args.warning:
        exit_plugin(
            STATE_WARNING,
            f"Key '{oldest_key}' is {oldest_age}s old "
            f"(warning threshold: {args.warning}s), "
            f"{num_keys} matching key(s) found",
            perfdata=perfdata,
        )

    exit_plugin(
        STATE_OK,
        f"{num_keys} key(s) matching '{args.pattern}', "
        f"oldest key '{oldest_key}' is {oldest_age}s old",
        perfdata=perfdata,
    )


if __name__ == "__main__":
    main()
