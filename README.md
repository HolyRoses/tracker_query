# tracker_query

`tracker_query.py` is a command-line BitTorrent tracker diagnostic client. It can send announce and scrape requests over HTTP, HTTPS, and UDP, inspect tracker responses, exercise retry and connection-ID behavior, and replay deterministic client lifecycles from JSON scenarios.

Use it only with trackers and swarms you are authorized to test. Announce requests affect tracker state just like requests from a BitTorrent client.

## Features

- HTTP, HTTPS, and BEP 15 UDP announce requests
- HTTP and UDP scrape requests, including multi-hash scrape
- Table, JSON, and CSV output
- Batch tracker checks, retries, and continuous loop mode
- BEP 34 DNS TXT endpoint discovery
- qBittorrent-like and BitComet-like client identities
- Gzip, deflate, Brotli, and Zstandard response handling
- Deterministic download, completion, restart, and seeding scenarios
- Secret-redacted JSON evidence output
- Token input through protected files or standard input

## Requirements

- Python 3.9 or newer
- No required third-party package for basic HTTP and UDP queries

Optional packages enable additional behavior:

```bash
python3 -m pip install dnspython brotli zstandard
```

- `dnspython`: native BEP 34 TXT lookups; the script can otherwise use `dig` or `nslookup`
- `brotli`: Brotli-compressed HTTP responses
- `zstandard`: Zstandard-compressed HTTP responses

## Quick Start

```bash
chmod +x tracker_query.py

# Query the default public UDP tracker with the default test hash.
./tracker_query.py

# Announce to a specific tracker.
./tracker_query.py \
  --tracker "https://tracker.example/announce" \
  --hash 0123456789abcdef0123456789abcdef01234567 \
  --event started

# Scrape one torrent and emit JSON.
./tracker_query.py \
  --tracker "https://tracker.example/announce" \
  --scrape \
  --hash 0123456789abcdef0123456789abcdef01234567 \
  --format json

# Run a lifecycle scenario without real-time waits and save evidence.
./tracker_query.py \
  --tracker "https://tracker.example/announce" \
  --scenario scenarios/download-to-completion.json \
  --scenario-time-scale 0 \
  --evidence-json evidence.json \
  --strict
```

## Scenario Mode

Scenario files describe a deterministic sequence of announces, simulated transfers, client downtime, and checkpoints. Transfer rates update cumulative byte counters and automatically emit a `completed` event when `left` reaches zero.

Included examples:

- `basic-start-stop.json`: open and close a short leecher session
- `download-to-completion.json`: download a fixed payload and announce completion
- `seed-session.json`: report a timed upload-only seeding session
- `resume-partial-download.json`: resume from existing counters and stop before completion
- `download-complete-restart-seed-200mbit.json`: complete, stop, restart, and seed

See [SCENARIOS.md](SCENARIOS.md) for the complete JSON structure, field constraints, assertion behavior, counter calculations, and token handling.

## Documentation

- [USAGE.md](USAGE.md): command modes, examples, and complete CLI reference
- [SCENARIOS.md](SCENARIOS.md): scenario JSON schema and lifecycle semantics

## Security Notes

- Never put an announce token directly in a scenario file or command-line URL.
- Prefer `--announce-token-file` with mode `0600`, or pipe the token through `--announce-token-stdin`.
- Tracker URLs and evidence output redact the value of a `token` query parameter.
- `--insecure` disables TLS certificate and hostname verification. Use it only for controlled test systems.
- Evidence files can contain info hashes, peer IDs, counters, tracker responses, and timestamps. Treat them as operational data.

## License

See [LICENSE](LICENSE).
