# Scenario JSON Reference

Scenario mode replays deterministic BitTorrent client lifecycles against an HTTP, HTTPS, or UDP announce endpoint. A scenario contains initial client state and an ordered set of actions. The runner records each generated announce and assertion in optional JSON evidence.

## Running a Scenario

```bash
./tracker_query.py \
  --tracker "https://tracker.example/announce" \
  --scenario scenarios/download-to-completion.json \
  --scenario-time-scale 0 \
  --evidence-json evidence.json \
  --strict
```

`--scenario-time-scale` changes wall-clock waiting without changing simulated counters:

- `1`: real time
- `0.5`: half of the declared wall time
- `0`: skip all waits while retaining the declared simulated durations and byte totals

## Root Object

```json
{
  "version": 1,
  "name": "example-scenario",
  "defaults": {},
  "steps": []
}
```

| Field | Required | Description |
| --- | --- | --- |
| `version` | yes | Schema version. It must be the integer `1`. |
| `name` | yes | Non-empty scenario name used in evidence. |
| `defaults` | no | Initial announce state. Defaults to an empty object. |
| `steps` | yes | Non-empty ordered array of actions. |

A scenario may contain at most 10,000 declared steps and may generate at most 10,000 announce operations. The combined declared duration of `transfer` and `sleep` actions may not exceed seven days.

Unknown object fields are currently ignored. Misspelled fields can therefore silently fall back to defaults; keep scenarios under version control and review evidence output.

## Defaults

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `info_hash` | string | built-in test hash | Exactly 40 hexadecimal characters. |
| `peer_id_ascii` | string | generated qBittorrent-like ID | ASCII and exactly 20 bytes. Mutually exclusive with `peer_id_hex`. |
| `peer_id_hex` | string | generated qBittorrent-like ID | Exactly 40 hexadecimal characters. Mutually exclusive with `peer_id_ascii`. |
| `port` | integer | `6881` | `1` through `65535`. |
| `key` | integer | random 32-bit value | `0` through `4294967295`. |
| `num_want` | integer | `0` | `0` through `2147483647`. |
| `uploaded` | integer | `0` | Cumulative bytes, `0` through `2^64 - 1`. |
| `downloaded` | integer | `0` | Cumulative bytes, `0` through `2^64 - 1`. |
| `left` | integer | `1000000000` | Remaining bytes, `0` through `2^64 - 1`. |

Use a fixed peer ID and key when the tracker must recognize every announce as the same client session.

Do not store an announce token in the JSON document. Supply it with `--announce-token-file` or `--announce-token-stdin`.

## Actions

Every step is an object with an `action`. The optional `name` is used in console and evidence output; otherwise the runner assigns `step-N`.

### `announce`

Sends one announce immediately.

```json
{
  "name": "start download",
  "action": "announce",
  "event": "started",
  "uploaded": 0,
  "downloaded": 0,
  "left": 104857600,
  "expect": {"success": true}
}
```

Fields:

- `event`: `started`, `none`, `completed`, or `stopped`; default `none`
- `uploaded`, `downloaded`, `left`: optional counter overrides applied before the request
- `expect`: optional assertion object; default `{"success": true}`

Counter overrides persist into later steps.

### `transfer`

Advances cumulative transfer counters and announces at each reporting interval.

```json
{
  "name": "download payload",
  "action": "transfer",
  "duration_sec": 10,
  "report_interval_sec": 5,
  "download_rate_mbps": 80,
  "upload_rate_mbps": 8,
  "stop_at_end": true,
  "expect": {"success": true},
  "stop_expect": {"success": true}
}
```

Required fields:

- `duration_sec`: positive decimal seconds
- `report_interval_sec`: positive decimal seconds

Optional fields:

- `download_rate_mbps`: decimal megabits per second; default `0`
- `upload_rate_mbps`: decimal megabits per second; default `0`
- `stop_at_end`: when true, send one additional `stopped` announce
- `expect`: assertions applied to every periodic report
- `stop_expect`: assertions applied to the generated stopped announce

Rates use decimal megabits: `bytes = Mbps * 1,000,000 * seconds / 8`. Fractional bytes carry into the next interval. Downloaded bytes never exceed `left`. When a report changes `left` from a positive value to zero, that report uses event `completed`; subsequent reports use event `none`.

### `sleep`

Advances simulated time without sending a request.

```json
{
  "name": "client offline",
  "action": "sleep",
  "duration_sec": 1800
}
```

`duration_sec` is a non-negative decimal. Wall-clock waiting is multiplied by `--scenario-time-scale`.

### `checkpoint`

Records the current counters and client identity in evidence without sending a request.

```json
{
  "name": "capture final counters",
  "action": "checkpoint"
}
```

## Assertions

An omitted `expect` is equivalent to:

```json
{"success": true}
```

Supported assertion forms:

- `success`: exact boolean request outcome
- `error_contains`: substring expected in a failed request error
- any other key: exact comparison against the protocol response object

HTTP announce responses can expose fields such as `status`, `interval`, `min_interval`, `complete`, `incomplete`, `downloaded`, `ipv4_peers`, `ipv6_peers`, `tracker_id`, and `response_encoding`. UDP responses expose `interval`, `leechers`, `seeders`, and `peer_bytes`. Both include `response_time_ms`, which is normally unsuitable for exact assertions.

Without `--strict`, the runner continues after failed assertions and exits `1` at the end. With `--strict`, it stops at the first failed assertion.

## Evidence Output

`--evidence-json PATH` writes the final report atomically with mode `0600`. Evidence includes:

- scenario name and version
- redacted tracker URL
- UTC start and finish timestamps
- simulated elapsed time
- each request's info hash, peer ID, port, key, event, and counters
- parsed tracker response or sanitized error
- assertion outcomes
- final summary and exit code

The token value and its URL-encoded representation are replaced with `<redacted>`. Evidence still contains operational identifiers and should not be published without review.

## Included Examples

| File | Purpose |
| --- | --- |
| `scenarios/basic-start-stop.json` | One started announce, a short offline period, and a stopped announce. |
| `scenarios/download-to-completion.json` | Downloads exactly 100 MB and exercises automatic completion. |
| `scenarios/seed-session.json` | Starts with `left: 0`, reports upload-only traffic, and stops. |
| `scenarios/resume-partial-download.json` | Resumes with existing counters, transfers more data, and stops while incomplete. |
| `scenarios/download-complete-restart-seed-200mbit.json` | Full download, stop, downtime, seeder restart, and sustained upload. |
