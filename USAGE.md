# Usage

## Basic Syntax

```bash
./tracker_query.py [options]
```

Without arguments, the script sends a `started` announce to its default UDP tracker using its default test info hash.

## Announce Requests

```bash
./tracker_query.py \
  --tracker "udp://tracker.example:6969/announce" \
  --hash 0123456789abcdef0123456789abcdef01234567 \
  --event started \
  --left 1073741824 \
  --num-want 50
```

`--event` accepts `started`, `completed`, `stopped`, or `none`. The `none` value omits an HTTP event parameter and sends event code zero over UDP.

Use `--left 0` to announce as a seeder. The single-request mode sends zero for uploaded and downloaded counters; scenario mode supports full cumulative counters.

## Output Formats

```bash
./tracker_query.py --tracker "https://tracker.example/announce" --format table
./tracker_query.py --tracker "https://tracker.example/announce" --format json
./tracker_query.py --tracker "https://tracker.example/announce" --format csv
```

Peer addresses are hidden unless `--show-peers` is enabled. Add `--lookup` to perform reverse DNS lookups for displayed peers.

## Scrape Requests

HTTP and HTTPS trackers support single-hash, multi-hash, and full scrape requests when the endpoint follows the conventional `announce` to `scrape` path mapping.

```bash
# Single hash
./tracker_query.py -t "https://tracker.example/announce" --scrape \
  -H 0123456789abcdef0123456789abcdef01234567

# Multiple hashes
./tracker_query.py -t "https://tracker.example/announce" --scrape \
  -H 0123456789abcdef0123456789abcdef01234567 \
  -H 89abcdef0123456789abcdef0123456789abcdef

# Full scrape
./tracker_query.py -t "https://tracker.example/announce" --full-scrape
```

UDP scrape requires at least one info hash and does not support full scrape.

## Batch Mode

Create a text file with one tracker URL per line. Blank lines and lines beginning with `#` are ignored.

```text
# trackers.txt
https://tracker-one.example/announce
udp://tracker-two.example:6969/announce
```

```bash
./tracker_query.py --batch --file trackers.txt --delay 1.5
```

Batch mode ignores `--tracker`.

## Retry and Loop Modes

Retry a single request until it succeeds:

```bash
./tracker_query.py -t "udp://tracker.example:6969/announce" --retry
```

Limit retry attempts:

```bash
./tracker_query.py -t "udp://tracker.example:6969/announce" --retry 5
```

Run repeated announces while retaining protocol state:

```bash
./tracker_query.py \
  -t "udp://tracker.example:6969/announce" \
  --loop \
  --interval 30 \
  --max-iterations 10
```

UDP loop mode reuses its connection ID until `--cid-client-max-age-sec` expires. `--bitcomet-mode` disables proactive connection-ID refresh and tracker-error reconnection while selecting a BitComet-like identity.

## BEP 34 Discovery

`--bep34` controls DNS TXT endpoint discovery:

- `off`: query only the endpoint in `--tracker`
- `prefer`: try TXT-advertised endpoints first, then the original endpoint
- `strict`: use only TXT-advertised endpoints

The default is `prefer`.

## HTTP Compression and TLS

`--accept-encoding` may be repeated or given a comma-separated list:

```bash
./tracker_query.py -t "https://tracker.example/announce" -a gzip -a br
./tracker_query.py -t "https://tracker.example/announce" -a gzip,zstd
```

Use `--insecure` only when intentionally testing a server with an untrusted certificate. It disables HTTPS certificate and hostname verification.

## Lifecycle Scenarios

```bash
./tracker_query.py \
  --tracker "https://tracker.example/announce" \
  --scenario scenarios/download-complete-restart-seed-200mbit.json \
  --scenario-time-scale 0 \
  --evidence-json evidence.json \
  --strict
```

Scenario mode cannot be combined with batch, loop, retry, or scrape mode. See [SCENARIOS.md](SCENARIOS.md) for the JSON schema.

### Private announce tokens

Read a token from a protected regular file:

```bash
printf '%s\n' 'replace-with-token' > announce-token.txt
chmod 600 announce-token.txt
./tracker_query.py \
  -t "https://tracker.example/announce" \
  --scenario scenarios/basic-start-stop.json \
  --announce-token-file announce-token.txt
```

Or read it from standard input:

```bash
printf '%s\n' 'replace-with-token' | ./tracker_query.py \
  -t "https://tracker.example/announce" \
  --scenario scenarios/basic-start-stop.json \
  --announce-token-stdin
```

The token is appended as a `token` query parameter and is redacted from normal output and evidence. Tokens are supported only for HTTP and HTTPS scenarios.

## Scenario Exit Codes

- `0`: scenario completed and all assertions passed
- `1`: at least one assertion failed
- `2`: invalid scenario, invalid arguments, or scenario setup error
- `4`: evidence output could not be written

Other command modes use nonzero status for invalid input, tracker failures, protocol failures, or exhausted retries.

## Complete Option Reference

The following is generated from `tracker_query.py --help`.

```text
usage: tracker_query.py [-h] [-b] [-t URL] [-H HEX] [-e EVENT] [-o FORMAT]
                        [-f FILE] [-p] [-l] [-r] [-n NUM] [-L BYTES]
                        [-d SECONDS] [--nocolor] [-k] [-a MODE] [-s]
                        [--full-scrape] [-R [COUNT]]
                        [--bep34 {off,prefer,strict}] [--loop]
                        [--interval SECONDS] [--max-iterations N]
                        [--cid-client-max-age-sec SECONDS]
                        [--retry-on-timeout N] [--retry-on-error N]
                        [--reconnect-on-tracker-error {on,off}]
                        [--bitcomet-mode] [--scenario PATH]
                        [--evidence-json PATH] [--scenario-time-scale FACTOR]
                        [--strict] [--announce-token-file PATH |
                        --announce-token-stdin] [--allow-insecure-token-file]

Query a BitTorrent tracker announce endpoint and display swarm info (seeds,
leechers, peers). Supports HTTP/HTTPS and UDP trackers, as well as scrape
requests.

options:
  -h, --help                    show this help message and exit
  -b, --batch                   Enable batch mode to query multiple trackers
                                from a file (ignores --tracker) (default:
                                False)
  -t, --tracker URL             Tracker announce URL (http://, https://, or
                                udp://). Ignored in batch mode. (default:
                                udp://tracker.opentrackr.org:6969/announce)
  -H, --hash HEX                Info hash (40 hex characters). Can be
                                specified multiple times for scrape mode to
                                query multiple torrents. (default: None)
  -e, --event EVENT             Announce event type (choices: started,
                                completed, stopped, none). Ignored in scrape
                                mode. (default: started)
  -o, --format FORMAT           Output format (choices: table, json, csv).
                                (default: table)
  -f, --file FILE               Tracker list file for batch mode (one tracker
                                URL per line, # for comments) (default:
                                trackers_to_query.txt)
  -p, --show-peers              Display the full list of peers (IP:port). Only
                                applies to announce mode. (default: False)
  -l, --lookup                  Perform reverse DNS lookup on peer IP
                                addresses. Requires --show-peers. IPv4-mapped
                                IPv6 addresses (::ffff:x.x.x.x) are handled
                                automatically. (default: False)
  -r, --random-qb               Use a random qBittorrent client version for
                                the announce (spoofs User-Agent and peer_id)
                                (default: False)
  -n, --num-want NUM            Number of peers to request from tracker.
                                Ignored in scrape mode. (default: 200)
  -L, --left BYTES              Bytes remaining to download. Defaults to 0 for
                                --event completed, 1000000000 otherwise. Use 0
                                to announce as a seeder. (default: None)
  -d, --delay SECONDS           Delay between queries in batch mode (in
                                seconds). Ignored in single-tracker mode.
                                (default: 1.0)
  --nocolor                     Disable colored output (useful for redirecting
                                to files) (default: False)
  -k, --insecure                Allow insecure HTTPS tracker connections
                                (disable TLS certificate and hostname
                                verification, like curl -k). (default: False)
  -a, --accept-encoding MODE    HTTP Accept-Encoding tokens (choices:
                                all,gzip,deflate,br,zstd,identity). Repeat or
                                comma-separate values (examples: -a br -a
                                gzip, -a br,gzip). If 'all' is included, it
                                overrides all other tokens. UDP unaffected.
                                (default: [])
  -s, --scrape                  Use scrape endpoint instead of announce.
                                Supports HTTP/HTTPS and UDP trackers (UDP
                                requires --hash; no full scrape). (default:
                                False)
  --full-scrape                 Scrape with no info_hash (implies --scrape).
                                Tests if tracker allows full scrape. (default:
                                False)
  -R, --retry [COUNT]           Retry connection until successful. Specify
                                COUNT for max attempts (e.g., --retry 5 or -R
                                5), or omit for infinite retries (e.g.,
                                --retry or -R). Only works in single-tracker
                                mode. (default: None)
  --bep34 {off,prefer,strict}   BEP34 DNS TXT handling: off=disabled,
                                prefer=try TXT-advertised endpoints first,
                                strict=only allow TXT-advertised endpoints.
                                (default: prefer)
  --loop                        Run continuously (single-tracker mode only).
                                For UDP trackers, reuses CID between requests.
                                (default: False)
  --interval SECONDS            Loop interval seconds when --loop is enabled.
                                (default: 5.0)
  --max-iterations N            Max loop cycles. 0 means infinite. (default:
                                0)
  --cid-client-max-age-sec SECONDS
                                Max client-side CID age before reconnect in
                                UDP loop mode. 0 means never refresh
                                proactively (BitComet-style). (default: 60)
  --retry-on-timeout N          Retries per loop cycle on timeout in loop
                                mode. -1 means infinite. (default: -1)
  --retry-on-error N            Retries per loop cycle on non-timeout errors
                                in loop mode. -1 means infinite. (default: -1)
  --reconnect-on-tracker-error {on,off}
                                On tracker error in UDP loop mode:
                                on=reconnect and recover where possible,
                                off=do not reconnect (BitComet-style).
                                (default: on)
  --bitcomet-mode               UDP loop convenience mode: sets --reconnect-
                                on-tracker-error off, --cid-client-max-age-sec
                                0, and BitComet-like identity (peer_id
                                -BC0220-...). (default: False)
  --scenario PATH               Run a version 1 deterministic lifecycle
                                scenario from JSON. (default: None)
  --evidence-json PATH          Atomically write secret-redacted scenario
                                evidence as JSON. (default: None)
  --scenario-time-scale FACTOR  Scenario wall-clock scale. 1 is real time, 0
                                skips waits while retaining simulated byte
                                totals. (default: 1.0)
  --strict                      Stop a scenario at its first failed assertion.
                                (default: False)
  --announce-token-file PATH    Read one announce token from a protected
                                regular file for scenario mode. (default:
                                None)
  --announce-token-stdin        Read one announce token from stdin for
                                scenario mode. (default: False)
  --allow-insecure-token-file   Allow group/other permissions on a synthetic
                                test token file. (default: False)
```
