# check_redis_keys_age

A Nagios/Icinga2 check plugin that monitors Redis keys by age.

The plugin connects to a Redis server, finds all keys whose names match a
regular expression, and verifies that the oldest matching key has been
updated recently enough. If the idle time of the oldest key exceeds the
configured warning or critical threshold, the check exits accordingly.

Key age is measured using the Redis `OBJECT IDLETIME` command, which returns
the number of seconds since a key was last read or written. This makes the
plugin well-suited for monitoring heartbeat keys, job timestamps, cache
entries, or any key that a background process is expected to refresh
periodically.

---

## Requirements

- Python 3.6 or later
- [`redis`](https://pypi.org/project/redis/) Python package (the only
  external dependency)

Install the dependency with:

```bash
pip install redis
```

---

## Installation

Copy the script to your Nagios/Icinga plugin directory and make it
executable:

```bash
cp check_redis_keys.py /usr/lib/nagios/plugins/check_redis_keys_age
chmod +x /usr/lib/nagios/plugins/check_redis_keys_age
```

---

## Usage

```
check_redis_keys_age -H <host> -p <pattern> [options]
```

### Required arguments

| Flag | Description |
|------|-------------|
| `-H`, `--host HOST` | Redis server hostname or IP address |
| `-p`, `--pattern PATTERN` | Regular expression matched against Redis key names |

### Optional arguments

| Flag | Default | Description |
|------|---------|-------------|
| `-P`, `--port PORT` | `6379` | Redis server port |
| `-D`, `--database DB` | `0` | Redis database number |
| `-w`, `--warning SECONDS` | — | Warn if the oldest matching key has been idle for at least this many seconds |
| `-c`, `--critical SECONDS` | — | Go critical if the oldest matching key has been idle for at least this many seconds |
| `-a`, `--password PASSWORD` | — | Redis `AUTH` password |
| `-t`, `--timeout SECONDS` | `10` | Connection and socket timeout |
| `-h`, `--help` | — | Show help and exit |

When both `-w` and `-c` are provided, `-w` must be strictly less than `-c`.

---

## Exit codes

The plugin follows the standard Nagios/Icinga exit code convention:

| Code | State | Meaning |
|------|-------|---------|
| `0` | `OK` | No matching keys found, or the oldest key is within both thresholds |
| `1` | `WARNING` | The oldest matching key has been idle for at least the warning threshold |
| `2` | `CRITICAL` | The oldest matching key has been idle for at least the critical threshold |
| `3` | `UNKNOWN` | Cannot connect to Redis, authentication failure, invalid regex, or other error |

When both thresholds are active, `CRITICAL` takes priority over `WARNING`.

If **no keys** match the pattern the plugin exits `OK` — a missing key is not
treated as an error. This lets you use the same check definition before a
process has written its first key without generating false alerts.

---

## Output format

The plugin writes a single line to stdout:

```
STATUS - <message> | <performance data>
```

### Examples of plugin output

```
OK - 3 key(s) matching 'heartbeat:.*', oldest key 'heartbeat:worker1' is 42s old | matched_keys=3;;;0; oldest_key_age=42s;300;600;0;
```

```
WARNING - Key 'heartbeat:worker1' is 320s old (warning threshold: 300s), 3 matching key(s) found | matched_keys=3;;;0; oldest_key_age=320s;300;600;0;
```

```
CRITICAL - Key 'heartbeat:worker1' is 650s old (critical threshold: 600s), 3 matching key(s) found | matched_keys=3;;;0; oldest_key_age=650s;300;600;0;
```

```
OK - No keys matching pattern 'heartbeat:.*' | matched_keys=0;;;0;
```

```
UNKNOWN - Cannot connect to Redis at 192.168.1.10:6379 - Connection refused.
```

### Performance data

| Label | Unit | Description |
|-------|------|-------------|
| `matched_keys` | — | Number of keys whose names matched the pattern |
| `oldest_key_age` | seconds (`s`) | Idle time of the oldest matching key |

Both metrics include warning/critical thresholds and a minimum of `0` so
that Icinga/Nagios graphing backends can plot them correctly.

---

## How key age is measured

Redis does not store a creation timestamp for keys. This plugin uses the
`OBJECT IDLETIME` command, which returns the number of seconds elapsed since
the key was last accessed (read **or** written).

For keys that are periodically refreshed by a process (heartbeats, job
markers, cache writes), idle time is an accurate measure of "time since last
update": if the process stops refreshing the key, the idle time grows until
it crosses the configured threshold.

> **Note:** If something else reads the key between check runs (another
> application, a `redis-cli` inspection, etc.) the idle time is reset. In
> such cases idle time reflects the time since the last *access* rather than
> the last *write*. Design your key usage accordingly if this distinction
> matters.

> **Note:** Some managed Redis services (e.g. Redis Cloud in certain
> configurations) disable `OBJECT IDLETIME`. The plugin will exit `UNKNOWN`
> with a descriptive message if the command is unavailable.

---

## Examples

### Basic heartbeat check (warning at 5 min, critical at 10 min)

```bash
check_redis_keys_age -H localhost -p "heartbeat:.*" -w 300 -c 600
```

### Non-default port and database

```bash
check_redis_keys_age -H redis.example.com -P 6380 -D 2 -p "job:backup:.*" -w 3600 -c 7200
```

### Password-protected Redis

```bash
check_redis_keys_age -H localhost -p "myapp:status:.*" -a s3cr3t -w 60 -c 120
```

### Check a specific key by exact name

```bash
check_redis_keys_age -H localhost -p "^scheduler:last_run$" -w 120 -c 300
```

### Only warn, no critical

```bash
check_redis_keys_age -H localhost -p "cache:.*" -w 600
```

### Report key age without any threshold (informational)

```bash
check_redis_keys_age -H localhost -p "myapp:.*"
```

---

## Icinga2 integration

### CheckCommand definition

A ready-to-use configuration file is provided in `check_redis_keys_age.conf`.
Copy it to your Icinga2 configuration directory:

```bash
cp check_redis_keys_age.conf /etc/icinga2/conf.d/check_redis_keys_age.conf
icinga2 daemon --validate
systemctl reload icinga2
```

### Custom variables

| Variable | Default | Description |
|----------|---------|-------------|
| `redis_keys_host` | `$address$` | Redis server hostname or IP address |
| `redis_keys_port` | `6379` | Redis server port |
| `redis_keys_database` | `0` | Redis database number |
| `redis_keys_pattern` | — | Regular expression matched against key names *(required)* |
| `redis_keys_warning` | — | Warning threshold in seconds |
| `redis_keys_critical` | — | Critical threshold in seconds |
| `redis_keys_password` | — | Redis AUTH password |
| `redis_keys_timeout` | `10` | Connection and socket timeout in seconds |

### Service definition example

```
apply Service "redis-heartbeat" {
    check_command = "check_redis_keys_age"

    vars.redis_keys_pattern  = "heartbeat:myapp:.*"
    vars.redis_keys_warning  = 300
    vars.redis_keys_critical = 600

    assign where host.vars.redis == true
}
```

---

## License

MIT