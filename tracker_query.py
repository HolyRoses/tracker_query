#!/usr/bin/env python3
"""
Tracker announce tester

Queries a BitTorrent tracker announce endpoint and shows
seeds / leechers / peer counts from the bencoded response.

Supports both HTTP/HTTPS and UDP trackers, as well as scrape requests.

Examples:
  ./tracker_test.py --tracker http://open.acgtracker.com:1096/announce
  ./tracker_test.py -t udp://tracker.opentrackr.org:1337/announce
  ./tracker_test.py -t http://tracker.example.net/announce --event completed
  ./tracker_test.py --tracker udp://tracker2.com:6969 --hash deadbeef... --event started
  ./tracker_test.py -t http://tracker.example.net/announce --format json --show-peers
  ./tracker_test.py -t http://tracker.example.net/announce --show-peers --lookup
  ./tracker_test.py -t http://tracker.example.net/announce --scrape
  ./tracker_test.py -t http://tracker.example.net/announce --scrape --hash deadbeef...
  ./tracker_test.py -t udp://flaky.tracker.com:1337/announce --retry
  ./tracker_test.py -t http://tracker.example.net/announce -R 5
"""

import sys
import argparse
import gzip
import zlib
import io
import urllib.parse
import urllib.request
import json
import socket
import struct
import random
import time
import os

try:
    import dns.resolver  # type: ignore
    from dns.exception import DNSException  # type: ignore
except Exception:
    dns = None
    DNSException = Exception

try:
    import brotli  # type: ignore
except Exception:
    brotli = None

try:
    import zstandard as zstd  # type: ignore
except Exception:
    zstd = None



# ────────────────────────────────────────────────
# Defaults
# ────────────────────────────────────────────────

# Global flag for color output (set by command-line argument)
NOCOLOR = False

DEFAULT_INFO_HASH_HEX = '5CB6C44712D494A87E8554839FB0541046B157AF'
DEFAULT_TRACKER       = 'udp://tracker.opentrackr.org:6969/announce'
DEFAULT_PEER_ID       = b'-qB5140-' + os.urandom(12)
DEFAULT_USER_AGENT    = "qBittorrent/5.1.4"
DEFAULT_TIMEOUT       = 12
DEFAULT_EVENT         = 'started'
DEFAULT_NUM_WANT      = 50
DEFAULT_LEFT          = 1_000_000_000
DEFAULT_ACCEPT_ENCODING = 'all'


def _nocolor_active(nocolor=None):
    """Resolve effective nocolor setting (explicit arg overrides global)."""
    if nocolor is None:
        return NOCOLOR
    return bool(nocolor)


def _color_palette(nocolor=None):
    """Return ANSI color palette with optional nocolor override."""
    colors = {
        'BRIGHT_GREEN': '\033[1;32m',
        'GREEN': '\033[0;32m',
        'YELLOW': '\033[1;33m',
        'RED': '\033[0;31m',
        'BLUE': '\033[0;34m',
        'NC': '\033[0m',
    }
    if _nocolor_active(nocolor):
        for k in colors:
            colors[k] = ''
    return colors


def _response_time_color_and_label(response_time, colors):
    """Map response time to (color, label)."""
    if response_time < 150:
        return colors['BRIGHT_GREEN'], "Excellent"
    if response_time < 300:
        return colors['GREEN'], "Good"
    if response_time < 500:
        return colors['YELLOW'], "OK"
    return colors['RED'], "Slow"

# qBittorrent version data for --random-qb
QB_VERSIONS = [
    ('4.1.9.1', '4191'),

    ('4.3.2',   '4320'),
    ('4.3.8',   '4380'),
    ('4.3.9',   '4390'),

    ('4.4.1',   '4410'),
    ('4.4.3.1', '4431'),
    ('4.4.5',   '4450'),

    ('4.5.0',   '4500'),
    ('4.5.2',   '4520'),
    ('4.5.5',   '4550'),

    ('4.6.3',   '4630'),
    ('4.6.4',   '4640'),
    ('4.6.5',   '4650'),
    ('4.6.6',   '4660'),
    ('4.6.7',   '4670'),

    ('5.0.2',   '5020'),
    ('5.0.3',   '5030'),
    ('5.0.4',   '5040'),
    ('5.0.5',   '5050'),

    ('5.1.0',   '5100'),
    ('5.1.1',   '5110'),
    ('5.1.2',   '5120'),
    ('5.1.3',   '5130'),
    ('5.1.4',   '5140'),
]

# UDP Protocol constants
UDP_ACTION_CONNECT  = 0
UDP_ACTION_ANNOUNCE = 1
UDP_ACTION_SCRAPE   = 2
UDP_PROTOCOL_ID     = 0x41727101980  # Magic constant for UDP trackers

# ────────────────────────────────────────────────────
# Client version helpers
# ────────────────────────────────────────────────────

def get_random_qb_client():
    """Select a random qBittorrent version and return (user_agent, peer_id)"""
    version, code = random.choice(QB_VERSIONS)
    user_agent = f"qBittorrent/{version}"
    peer_id = f"-qB{code}-".encode('ascii') + os.urandom(12)
    return user_agent, peer_id

# ────────────────────────────────────────────────────
# Scrape URL conversion
# ────────────────────────────────────────────────────

def convert_announce_to_scrape(announce_url):
    """
    Convert an announce URL to a scrape URL following the BitTorrent scrape convention.

    Returns (scrape_url, error_message) tuple.
    If conversion fails, scrape_url is None and error_message explains why.
    """
    # Parse the URL
    parsed = urllib.parse.urlparse(announce_url)

    # Scrape only works with HTTP/HTTPS
    if parsed.scheme not in ('http', 'https'):
        return None, "Scrape is only supported for HTTP/HTTPS trackers"

    # Find the last '/' in the path
    path = parsed.path
    if not path or path == '/':
        return None, "Invalid announce URL: no path component"

    last_slash_idx = path.rfind('/')
    if last_slash_idx == -1:
        return None, "Invalid announce URL: no '/' found in path"

    # Get the part after the last '/'
    after_slash = path[last_slash_idx + 1:]

    # Check if it starts with 'announce'
    if not after_slash.startswith('announce'):
        return None, f"Scrape not supported: path doesn't contain 'announce' after last '/' (found '{after_slash}')"

    # Replace 'announce' with 'scrape'
    scrape_component = 'scrape' + after_slash[len('announce'):]
    scrape_path = path[:last_slash_idx + 1] + scrape_component

    # Reconstruct the URL
    scrape_url = urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        scrape_path,
        parsed.params,
        parsed.query,  # Keep original query params
        parsed.fragment
    ))

    return scrape_url, None


def _bep34_parse_txt_record(record_text):
    """
    Parse a BEP34 TXT record.
    Returns list of (proto, port) preferences in record order.
    Unknown tokens are ignored.
    """
    prefs = []
    tokens = record_text.split()
    # BEP34 sentinel is specified as exact uppercase "BITTORRENT".
    if not tokens or tokens[0] != 'BITTORRENT':
        return prefs
    for token in tokens[1:]:
        upper = token.upper()
        if upper.startswith('UDP:'):
            port_s = token[4:]
            if port_s.isdigit():
                port = int(port_s)
                if 1 <= port <= 65535:
                    prefs.append(('udp', port))
        elif upper.startswith('TCP:'):
            port_s = token[4:]
            if port_s.isdigit():
                port = int(port_s)
                if 1 <= port <= 65535:
                    prefs.append(('tcp', port))
    return prefs


def _bep34_lookup_prefs(hostname):
    """
    Returns (has_bep34_record, prefs, error_message).
    - has_bep34_record=True means a BITTORRENT TXT record exists.
    - prefs is ordered protocol preferences (may be empty to explicitly deny trackers).
    """
    if not hostname:
        return False, [], None
    if dns is None:
        return False, [], "dnspython not installed (pip install dnspython)"
    try:
        answer = dns.resolver.resolve(hostname, "TXT")
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        # No TXT answer / host missing -> treat as no BEP34 record, not a warning-worthy failure.
        return False, [], None
    except DNSException as e:
        return False, [], f"DNS TXT lookup failed: {e}"

    try:
        for rdata in answer:
            try:
                # dnspython may return split strings; join then decode to text.
                # If one unrelated TXT record is malformed, skip it and keep searching.
                parts = []
                for p in getattr(rdata, "strings", []):
                    if isinstance(p, bytes):
                        parts.append(p.decode("utf-8", errors="ignore"))
                    else:
                        parts.append(str(p))
                record_text = "".join(parts).strip()
            except Exception:
                continue
            if record_text.startswith("BITTORRENT"):
                # Keep parse outside the decode/shape try-block so future parser changes
                # do not silently turn a detected BEP34 record into "no record".
                return True, _bep34_parse_txt_record(record_text), None
    except Exception as e:
        return False, [], f"DNS TXT iteration failed: {e}"
    return False, [], None


def _bep34_build_candidate_urls(tracker_url, prefs):
    parsed = urllib.parse.urlparse(tracker_url)
    host = parsed.hostname
    if not host:
        return []
    # urlparse().hostname strips IPv6 brackets; add them back for netloc rebuild.
    host_for_netloc = f"[{host}]" if ':' in host and not host.startswith('[') else host
    path = parsed.path or "/announce"
    candidates = []
    for proto, port in prefs:
        netloc = f"{host_for_netloc}:{port}"
        if proto == 'udp':
            candidate = urllib.parse.urlunparse(('udp', netloc, path, '', '', ''))
        else:
            # For TCP preference, keep secure scheme if original was https; otherwise use http.
            scheme = 'https' if parsed.scheme.lower() == 'https' else 'http'
            candidate = urllib.parse.urlunparse((scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _bep34_resolve_targets(tracker_url, bep34_mode):
    """
    Return (targets, strict_record_seen, error_message)
    targets: tracker URLs to attempt in order.
    """
    mode = (bep34_mode or 'off').lower()
    if mode == 'off':
        return [tracker_url], False, None

    parsed = urllib.parse.urlparse(tracker_url)
    has_record, prefs, err = _bep34_lookup_prefs(parsed.hostname)

    if mode == 'strict':
        if err:
            return [], has_record, f"BEP34 strict mode: {err}"
        if not has_record:
            return [], False, "BEP34 strict mode: no BITTORRENT TXT record found"
        if not prefs:
            return [], True, "BEP34 strict mode: host denies trackers (BITTORRENT with no endpoints)"
        return _bep34_build_candidate_urls(tracker_url, prefs), True, None

    # prefer mode
    if err:
        return [tracker_url], False, f"BEP34 prefer mode fallback: {err}"
    if has_record:
        candidates = _bep34_build_candidate_urls(tracker_url, prefs)
        if candidates:
            # Try BEP34-advertised endpoint(s), then fallback to submitted URL.
            return candidates + [tracker_url], True, None
        # Explicit deny/no endpoints -> fallback to original in prefer mode.
        return [tracker_url], True, None

    # No BEP34 record in prefer mode: best effort fallback.
    return [tracker_url], False, None


class _SysExitFailure(Exception):
    """Internal marker for per-target sys.exit(non-zero) failures."""
    pass


def _should_emit_attempt_error(last_error):
    """Emit only meaningful exceptions; suppress internal sys.exit markers."""
    return last_error is not None and not isinstance(last_error, _SysExitFailure)


def _attempt_tracker_targets(
    targets, info_hash_hex, event, output_format, show_peers,
    user_agent, peer_id, num_want, scrape=False, lookup_dns=False,
    left=DEFAULT_LEFT, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None
):
    """
    Try tracker targets in order and return (success, response_time, last_error).
    Catches SystemExit per-target so BEP34 fallbacks are attempted.
    """
    last_error = None
    for target in targets:
        try:
            response_time = _test_tracker_impl(
                target, info_hash_hex, event, output_format, show_peers,
                user_agent, peer_id, num_want, scrape, lookup_dns, left, accept_encoding, nocolor
            )
            return True, response_time, None
        except SystemExit as e:
            if e.code == 0:
                return True, None, None
            # _test_tracker_impl prints its own user-facing failure context before sys.exit().
            # Keep a sentinel for control flow but avoid adding noisy duplicate error output.
            last_error = _SysExitFailure()
            continue
        except Exception as e:
            last_error = e
            continue
    return False, None, last_error

# ────────────────────────────────────────────────
# Simple bencode decoder
# ────────────────────────────────────────────────

def bdecode(data):
    def decode(i):
        b = data[i]
        if b == ord('i'):
            end = data.index(b'e', i)
            return int(data[i+1:end]), end + 1
        elif b == ord('l'):
            items = []
            i += 1
            while data[i] != ord('e'):
                val, i = decode(i)
                items.append(val)
            return items, i + 1
        elif b == ord('d'):
            d = {}
            i += 1
            while data[i] != ord('e'):
                key, i = decode(i)
                val, i = decode(i)
                d[key] = val
            return d, i + 1
        elif 48 <= b <= 57:  # byte-string length must start with a digit
            colon = data.index(b':', i)
            length_str = data[i:colon].decode('ascii')
            length = int(length_str)
            start = colon + 1
            end = start + length
            if end > len(data):
                raise ValueError(f"Invalid bencode: string length out of bounds at {i} (len={length})")
            return data[start:end], end
        else:
            raise ValueError(f"Unexpected byte at {i}: {chr(b) if 32 <= b <= 126 else hex(b)}")

    result, _ = decode(0)
    return result

# ────────────────────────────────────────────────
# Peer decoding (shared by HTTP and UDP)
# ────────────────────────────────────────────────

def decode_compact_peers_ipv4(data):
    """Decode compact binary peer format (IPv4): 6 bytes per peer"""
    peers = []
    for i in range(0, len(data), 6):
        if i + 6 > len(data):
            break
        ip_bytes = data[i:i+4]
        port_bytes = data[i+4:i+6]
        
        ip = socket.inet_ntoa(ip_bytes)
        port = struct.unpack('!H', port_bytes)[0]
        peers.append({'ip': ip, 'port': port, 'type': 'ipv4'})
    
    return peers

def decode_compact_peers_ipv6(data):
    """Decode compact binary peer format (IPv6): 18 bytes per peer"""
    peers = []
    for i in range(0, len(data), 18):
        if i + 18 > len(data):
            break
        ip_bytes = data[i:i+16]
        port_bytes = data[i+16:i+18]
        
        ip = socket.inet_ntop(socket.AF_INET6, ip_bytes)
        port = struct.unpack('!H', port_bytes)[0]

        # Detect IPv4-mapped IPv6 addresses (::ffff:x.x.x.x)
        peer_type = 'ipv4-mapped' if ip.startswith('::ffff:') else 'ipv6'

        peers.append({'ip': ip, 'port': port, 'type': peer_type})

    return peers

def decode_dict_peers(peer_list):
    """Decode dictionary format peers"""
    peers = []
    for peer in peer_list:
        if isinstance(peer, dict):
            ip = peer.get(b'ip', b'').decode('utf-8', errors='replace')
            port = peer.get(b'port', 0)
            peer_id = peer.get(b'peer id', b'')
            
            peer_info = {'ip': ip, 'port': port, 'type': 'dict'}
            if peer_id:
                peer_info['peer_id'] = peer_id.hex()
            peers.append(peer_info)

    return peers

# ────────────────────────────────────────────────
# DNS Lookup Functions
# ────────────────────────────────────────────────

def extract_ipv4_from_mapped(ip):
    """
    Extract IPv4 address from IPv4-mapped IPv6 address (::ffff:x.x.x.x)
    Returns the IPv4 address string if it's mapped, otherwise returns None
    """
    if ip.startswith('::ffff:'):
        return ip[7:]  # Remove '::ffff:' prefix
    return None

def reverse_dns_lookup(ip):
    """
    Perform reverse DNS lookup on an IP address.
    For IPv4-mapped IPv6 addresses (::ffff:x.x.x.x), extracts and looks up the IPv4 address.
    Returns the DNS name if found, otherwise returns the original IP.
    """
    # Check if this is an IPv4-mapped IPv6 address
    ipv4_addr = extract_ipv4_from_mapped(ip)
    lookup_ip = ipv4_addr if ipv4_addr else ip

    try:
        # Perform reverse DNS lookup
        hostname = socket.gethostbyaddr(lookup_ip)[0]
        return hostname
    except (socket.herror, socket.gaierror, OSError):
        # Lookup failed, return original IP
        return ip

def apply_dns_lookup_to_peers(peer_list):
    """
    Apply DNS lookup to all peers in the list.
    Modifies the peer dictionaries in-place, adding a 'hostname' field.
    """
    for peer in peer_list:
        peer['hostname'] = reverse_dns_lookup(peer['ip'])

# ────────────────────────────────────────────────
# Output formatting (shared by HTTP and UDP)
# ────────────────────────────────────────────────

def format_table_output(data, show_peers=False, lookup_dns=False, nocolor=None):
    """Format data as a clean aligned table"""
    colors = _color_palette(nocolor)
    BRIGHT_GREEN = colors['BRIGHT_GREEN']
    GREEN = colors['GREEN']
    YELLOW = colors['YELLOW']
    RED = colors['RED']
    NC = colors['NC']

    print("\nTracker Response Summary:")
    print("─" * 50)

    # Display response time with color coding
    response_time = data.get('response_time_ms')
    if response_time is not None:
        color, speed = _response_time_color_and_label(response_time, colors)
        print(f"Response Time:     {color}{response_time:>10.2f} ms ({speed}){NC}")
    else:
        print(f"Response Time:     {'N/A':>10}")
    if data.get('response_encoding'):
        print(f"Response Encoding: {data['response_encoding']:>10}")

    # Display warning message if present
    # example Warning using http://nyaa.tracker.wf:7777/announce
    if data.get('warning_message'):
        print(f"{YELLOW}⚠ Warning:         {data['warning_message']}{NC}")

    # Display failure reason if present
    # example Failure using http://ch3oh.ru:6969/announce
    if data.get('failure_reason'):
        print(f"{RED}✗ Failure:         {data['failure_reason']}{NC}")

    # Display external IP if present (BEP 24)
    # IPv4 reply from http://tracker.skyts.net:6969/announce
    # IPv6 reply from http://tracker.ghostchu-services.top:80/announce
    if data.get('external_ip'):
        print(f"External IP:       {data['external_ip']}")

    # Display tracker ID if present
    # Currently supported @ http://tracker.skyts.net:6969/announce
    if data.get('tracker_id'):
        print(f"Tracker ID:        {data['tracker_id']}")

    print(f"Interval:          {data['interval']:>10} s")
    print(f"Min Interval:      {data['min_interval']:>10} s")
    print(f"Seeds:             {data['seeds']:>10}")
    print(f"Leechers:          {data['leechers']:>10}")
    print(f"Times Downloaded:  {data['downloaded']:>10}")
    print(f"IPv4 Peers:        {data['ipv4_peers']:>10} ({data['ipv4_bytes']} bytes)")
    print(f"IPv6 Peers:        {data['ipv6_peers']:>10} ({data['ipv6_bytes']} bytes)")

    # Check if tracker respects num_want
    if data.get('num_want_requested') and data.get('total_peers_returned'):
        requested = data['num_want_requested']
        returned = data['total_peers_returned']
        if returned > requested:
            print(f"⚠ Requested:       {requested:>10} peers (tracker returned {returned}, ignores num_want)")
        else:
            print(f"Requested:         {requested:>10} peers (respected)")

    print("─" * 50)

    if show_peers and data.get('peer_list'):
        print("\nPeer List:")
        print("─" * 50)
        for i, peer in enumerate(data['peer_list'], 1):
            peer_id_info = f" | ID: {peer['peer_id'][:16]}..." if 'peer_id' in peer else ""
            # Use hostname if lookup was performed, otherwise use IP
            display_addr = peer.get('hostname', peer['ip']) if lookup_dns else peer['ip']
            print(f"{i:3d}. {display_addr:39s}:{peer['port']:<5d} [{peer['type']}]{peer_id_info}")
        print("─" * 50)

def format_json_output(data, show_peers=False, lookup_dns=False):
    """Format data as JSON"""
    if not show_peers:
        # Remove peer_list from output if not requested
        data = {k: v for k, v in data.items() if k != 'peer_list'}
    print(json.dumps(data, indent=2))

def format_csv_output(data, show_peers=False, lookup_dns=False):
    """Format data as CSV"""
    keys = ['tracker', 'response_time_ms', 'interval', 'min_interval', 'seeds', 'leechers', 'downloaded',
            'ipv4_peers', 'ipv6_peers', 'warning_message', 'failure_reason', 'external_ip', 'tracker_id']
    print(",".join(keys))
    print(",".join(str(data.get(k, '?')) for k in keys))
    
    if show_peers and data.get('peer_list'):
        if lookup_dns:
            print("\nhostname,ip,port,type,peer_id")
            for peer in data['peer_list']:
                peer_id = peer.get('peer_id', '')
                hostname = peer.get('hostname', peer['ip'])
                print(f"{hostname},{peer['ip']},{peer['port']},{peer['type']},{peer_id}")
        else:
            print("\nip,port,type,peer_id")
            for peer in data['peer_list']:
                peer_id = peer.get('peer_id', '')
                print(f"{peer['ip']},{peer['port']},{peer['type']},{peer_id}")


def format_scrape_csv_output(torrents):
    """Format scrape torrent stats as CSV rows."""
    print("info_hash,complete,incomplete,downloaded,name")
    for torrent in torrents:
        name = str(torrent.get('name', '') or '').replace(',', ';')
        print(
            f"{torrent.get('info_hash', '')},{torrent.get('complete', 0)},"
            f"{torrent.get('incomplete', 0)},{torrent.get('downloaded', 0)},{name}"
        )


def format_scrape_table_output(data, nocolor=None):
    """Format scrape data as a clean aligned table"""
    colors = _color_palette(nocolor)
    BRIGHT_GREEN = colors['BRIGHT_GREEN']
    GREEN = colors['GREEN']
    YELLOW = colors['YELLOW']
    RED = colors['RED']
    NC = colors['NC']

    print("\nScrape Response Summary:")
    print("─" * 50)

    # Display response time with color coding
    response_time = data.get('response_time_ms')
    if response_time is not None:
        color, speed = _response_time_color_and_label(response_time, colors)
        print(f"Response Time:     {color}{response_time:>10.2f} ms ({speed}){NC}")
    else:
        print(f"Response Time:     {'N/A':>10}")
    if data.get('response_encoding'):
        print(f"Response Encoding: {data['response_encoding']:>10}")

    # Display failure reason if present
    if data.get('failure_reason'):
        print(f"{RED}✗ Failure:         {data['failure_reason']}{NC}")

    # Display min_request_interval if present (unofficial extension)
    # Example return data from http://1337.abcvg.info:80/announce or http://ftp.pet:6969/announce
    if data.get('min_request_interval'):
        print(f"Min Request Int:   {data['min_request_interval']:>10} s")

    print(f"Torrents Found:    {data['torrent_count']:>10}")
    print("─" * 50)

    # Display torrents
    if data.get('torrents'):
        print("\nTorrent Statistics:")
        print("─" * 50)
        for i, torrent in enumerate(data['torrents'], 1):
            print(f"\nTorrent #{i}:")
            print(f"  Info Hash:       {torrent['info_hash']}")
            if torrent.get('name'):
                print(f"  Name:            {torrent['name']}")
            print(f"  Seeds (complete):      {torrent['complete']:>5}")
            print(f"  Leechers (incomplete): {torrent['incomplete']:>5}")
            print(f"  Times Downloaded:      {torrent['downloaded']:>5}")
        print("─" * 50)

# ────────────────────────────────────────────────
# HTTP Tracker Functions
# ────────────────────────────────────────────────

def percent_encode_bytes(data: bytes) -> str:
    """
    Strict BitTorrent-safe percent encoding.
    Encodes EVERY byte as %XX (uppercase).
    """
    return ''.join(f'%{b:02X}' for b in data)


def parse_info_hash(hex_str, include_input=False):
    """Parse and validate a BEP 3 info hash (20-byte SHA-1 hex)."""
    info_hash_bytes = bytes.fromhex(hex_str)
    if len(info_hash_bytes) != 20:
        if include_input:
            raise ValueError(f"Info hash must be exactly 40 hex characters (20 bytes): {hex_str}")
        raise ValueError("Info hash must be exactly 40 hex characters (20 bytes)")
    return info_hash_bytes

def build_announce_url(tracker_url, info_hash_bytes, event, peer_id, num_want, left=DEFAULT_LEFT):
    params = {
        'peer_id':     peer_id,
        'port':        '6881',
        'uploaded':    '0',
        'downloaded':  '0',
        'left':        str(left),
        'compact':     '1',
        'no_peer_id':  '1',
        'numwant':     str(num_want),
    }

    # Only include event when it's one of the explicit ones
    if event in ('started', 'completed', 'stopped'):
        params['event'] = event
    # else: omit entirely → means "none" / regular announce

    # Encode everything except info_hash normally
    query = urllib.parse.urlencode(params, doseq=False, safe='~')

    # Strictly encode info_hash
    info_hash_encoded = percent_encode_bytes(info_hash_bytes)

    return f"{tracker_url}?info_hash={info_hash_encoded}&{query}"


def build_accept_encoding_header(mode):
    """
    Build Accept-Encoding header value from one or more CLI values.
    Supported tokens: all, gzip, deflate, br, zstd, identity
    Precedence: if 'all' is present anywhere, it overrides all other tokens.
    Input examples:
      - 'all'
      - 'br,gzip'
      - ['br', 'gzip']
      - ['br,gzip', 'zstd']
    """
    raw_values = mode if isinstance(mode, (list, tuple)) else [mode]
    tokens = []
    for raw in raw_values:
        part = (raw or '').strip()
        if not part:
            continue
        for t in part.split(','):
            tok = t.strip().lower()
            if tok:
                tokens.append(tok)

    if not tokens:
        tokens = [DEFAULT_ACCEPT_ENCODING]

    if 'all' in tokens:
        parts = ['gzip', 'deflate']
        if brotli is not None:
            parts.append('br')
        if zstd is not None:
            parts.append('zstd')
        return ', '.join(parts)

    allowed = {'gzip', 'deflate', 'br', 'zstd', 'identity'}
    out = []
    seen = set()
    for tok in tokens:
        if tok not in allowed:
            raise ValueError(
                f"Invalid --accept-encoding token '{tok}'. "
                "Valid tokens: all, gzip, deflate, br, zstd, identity"
            )
        if tok == 'br' and brotli is None:
            raise ValueError(
                "Cannot request 'br' encoding: 'brotli' module is not installed"
            )
        if tok == 'zstd' and zstd is None:
            raise ValueError(
                "Cannot request 'zstd' encoding: 'zstandard' module is not installed"
            )
        if tok not in seen:
            out.append(tok)
            seen.add(tok)
    return ', '.join(out)


def decode_http_body_for_content_encoding(body, content_encoding):
    enc = (content_encoding or '').strip().lower()
    if not enc:
        return body
    if 'gzip' in enc:
        return gzip.decompress(body)
    if 'deflate' in enc:
        try:
            # RFC-compliant zlib-wrapped deflate.
            return zlib.decompress(body)
        except zlib.error:
            # Fallback for non-compliant raw deflate streams.
            return zlib.decompress(body, -zlib.MAX_WBITS)
    if 'br' in enc:
        if brotli is None:
            raise ValueError("Received br-encoded response but 'brotli' module is not installed")
        return brotli.decompress(body)
    if 'zstd' in enc:
        if zstd is None:
            raise ValueError("Received zstd-encoded response but 'zstandard' module is not installed")
        dctx = zstd.ZstdDecompressor()
        try:
            return dctx.decompress(body)
        except zstd.ZstdError:
            # Some trackers emit frames without content size in header.
            # Fall back to streaming decode for compatibility.
            with dctx.stream_reader(io.BytesIO(body)) as reader:
                return reader.read()
    return body


def normalize_response_encoding(content_encoding):
    enc = (content_encoding or '').strip().lower()
    if not enc:
        return 'identity'
    return enc.split(',', 1)[0].strip() or 'identity'

def test_http_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, lookup_dns=False, left=DEFAULT_LEFT, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None):
    """Test HTTP/HTTPS tracker and return response time in milliseconds"""
    start_time = time.time()

    try:
        info_hash_bytes = parse_info_hash(info_hash_hex)
    except ValueError as e:
        print(f"Error: Invalid info hash — {e}", file=sys.stderr)
        sys.exit(2)

    url = build_announce_url(tracker_url, info_hash_bytes, event, peer_id, num_want, left)

    # Only print headers for table format (not for json/csv)
    if output_format == 'table':
        print(f"\n{'─' * 50}")
        print(f"HTTP {event.upper()} → {tracker_url}")
        print(f"{'─' * 50}")
        print(f"Client: {user_agent}")
        print(f"URL: {url[:140]}{'...' if len(url) > 140 else ''}")

    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': user_agent,
            'Accept-Encoding': accept_encoding,
        },
        method='GET'
    )

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            response_time_ms = (time.time() - start_time) * 1000
            status = resp.getcode()
            body = resp.read()

            content_encoding = resp.getheader('Content-Encoding', '')
            body = decode_http_body_for_content_encoding(body, content_encoding)
            response_encoding = normalize_response_encoding(content_encoding)

            # Only print status for table format
            if output_format == 'table':
                print(f"Status: {status}   Size: {len(body)} bytes   Response time: {response_time_ms:.2f}ms")

            if status != 200:
                if output_format == 'table':
                    print("Non-200 response — tracker likely dead or blocked")
                    if body:
                        print("Body preview:", body[:200].decode('ascii', errors='replace'))
                sys.exit(1)

            try:
                decoded = bdecode(body)
                if not isinstance(decoded, dict):
                    if output_format == 'table':
                        print("Response is not a bencoded dictionary")
                    sys.exit(1)

                # Extract failure reason if present (normalized naming)
                failure_reason = decoded.get(b'failure reason', b'').decode('utf-8', errors='replace')
                if not failure_reason:
                    failure_reason = None

                # Extract warning message if present (normalized naming)
                warning_message = decoded.get(b'warning message', b'').decode('utf-8', errors='replace')
                if not warning_message:
                    warning_message = None

                # Extract external IP if present (BEP 24)
                # BEP 24: IPv4 = 32-bit binary (4 bytes), IPv6 = 128-bit binary (16 bytes)
                external_ip = decoded.get(b'external ip', b'')
                if isinstance(external_ip, bytes) and len(external_ip) == 4:
                    # IPv4: 32-bit binary
                    external_ip = socket.inet_ntoa(external_ip)
                elif isinstance(external_ip, bytes) and len(external_ip) == 16:
                    # IPv6: 128-bit binary
                    external_ip = socket.inet_ntop(socket.AF_INET6, external_ip)
                elif isinstance(external_ip, bytes) and len(external_ip) > 0:
                    # Non-standard: try decoding as string (shouldn't happen per BEP 24)
                    external_ip = external_ip.decode('utf-8', errors='replace')
                else:
                    external_ip = None

                # Extract tracker ID if present
                tracker_id = decoded.get(b'tracker id', b'')
                if isinstance(tracker_id, bytes):
                    tracker_id = tracker_id.decode('utf-8', errors='replace')
                if not tracker_id:
                    tracker_id = None

                interval     = decoded.get(b'interval',     '?')
                min_int      = decoded.get(b'min interval', '?')
                seeds        = decoded.get(b'complete',     0)
                leechers     = decoded.get(b'incomplete',   0)
                downloaded   = decoded.get(b'downloaded',   '?')

                peers_ipv4 = decoded.get(b'peers', b'')
                peers_ipv6 = decoded.get(b'peers6', b'')

                # Detect if we connected via IPv6 — same approach as UDP:
                # resolve the hostname and check the address family.
                # Literal IPv6 brackets e.g. http://[2601:...] are handled
                # automatically since getaddrinfo resolves them correctly too.
                try:
                    _parsed = urllib.parse.urlparse(tracker_url)
                    _ai = socket.getaddrinfo(_parsed.hostname, None, socket.AF_UNSPEC)
                    tracker_is_ipv6 = bool(_ai) and _ai[0][0] == socket.AF_INET6
                except (socket.gaierror, OSError):
                    tracker_is_ipv6 = False  # can't resolve — assume IPv4

                # Warn if IPv6 client received IPv4 peers with no peers6 —
                # same validation the UDP path performs via is_ipv6
                if tracker_is_ipv6 and output_format == 'table':
                    has_ipv4 = isinstance(peers_ipv4, bytes) and len(peers_ipv4) > 0 or isinstance(peers_ipv4, list) and len(peers_ipv4) > 0
                    has_ipv6 = isinstance(peers_ipv6, bytes) and len(peers_ipv6) > 0 or isinstance(peers_ipv6, list) and len(peers_ipv6) > 0
                    if has_ipv4 and not has_ipv6:
                        print('Warning: Connected via IPv6 but received IPv4 peers response (no peers6 field)')

                # Decode peers
                peer_list = []

                # Handle compact IPv4 peers (binary format)
                if isinstance(peers_ipv4, bytes) and len(peers_ipv4) > 0:
                    peer_list.extend(decode_compact_peers_ipv4(peers_ipv4))
                # Handle dictionary format peers
                elif isinstance(peers_ipv4, list):
                    peer_list.extend(decode_dict_peers(peers_ipv4))

                # Handle compact IPv6 peers (binary format)
                if isinstance(peers_ipv6, bytes) and len(peers_ipv6) > 0:
                    peer_list.extend(decode_compact_peers_ipv6(peers_ipv6))
                # Handle dictionary format IPv6 peers
                elif isinstance(peers_ipv6, list):
                    peer_list.extend(decode_dict_peers(peers_ipv6))

                ipv4_count = len(peers_ipv4) // 6 if isinstance(peers_ipv4, bytes) else len(peers_ipv4) if isinstance(peers_ipv4, list) else 0
                ipv6_count = len(peers_ipv6) // 18 if isinstance(peers_ipv6, bytes) else len(peers_ipv6) if isinstance(peers_ipv6, list) else 0
                total_peers_returned = len(peer_list)

                # Perform DNS lookup if requested
                if lookup_dns and show_peers and peer_list:
                    apply_dns_lookup_to_peers(peer_list)

                data = {
                    'tracker': tracker_url,
                    'interval': interval,
                    'min_interval': min_int,
                    'seeds': seeds,
                    'leechers': leechers,
                    'downloaded': downloaded,
                    'ipv4_peers': ipv4_count,
                    'ipv4_bytes': len(peers_ipv4) if isinstance(peers_ipv4, bytes) else 0,
                    'ipv6_peers': ipv6_count,
                    'ipv6_bytes': len(peers_ipv6) if isinstance(peers_ipv6, bytes) else 0,
                    'peer_list': peer_list,
                    'num_want_requested': num_want,
                    'total_peers_returned': total_peers_returned,
                    'response_time_ms': round(response_time_ms, 2),
                    'response_encoding': response_encoding,
                    'warning_message': warning_message,
                    'failure_reason': failure_reason,
                    'external_ip': external_ip,
                    'tracker_id': tracker_id
                }

                if output_format == 'json':
                    format_json_output(data, show_peers, lookup_dns)
                elif output_format == 'csv':
                    format_csv_output(data, show_peers, lookup_dns)
                else:  # table
                    format_table_output(data, show_peers, lookup_dns, nocolor=nocolor)

                # Exit with error if there was a failure reason
                if failure_reason:
                    sys.exit(1)

            except Exception as e:
                if output_format == 'table':
                    print(f"Bdecode error: {str(e)}")
                    print("Raw preview (first 160 bytes):")
                    print(body[:160].hex(' ', -1))
                sys.exit(1)

    except urllib.error.HTTPError as e:
        if output_format == 'table':
            print(f"HTTP Error: {e.code} {e.reason}")
        sys.exit(1)
    except Exception as e:
        if output_format == 'table':
            print(f"Request failed: {type(e).__name__}: {str(e)}")
        sys.exit(1)

    # Return response time for batch mode tracking
    return round(response_time_ms, 2)

def test_http_scrape(tracker_url, info_hash_hex, output_format, user_agent, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None):
    """Test HTTP/HTTPS tracker scrape endpoint and return response time in milliseconds

    info_hash_hex can be:
    - A single hex string
    - A list of hex strings (for multi-hash scrape)
    """
    start_time = time.time()

    # Convert announce URL to scrape URL
    scrape_url, error = convert_announce_to_scrape(tracker_url)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(2)

    # Handle full scrape (no hash), single hash, or multiple hashes
    if info_hash_hex is None:
        # Full scrape — no info_hash parameter sent
        full_url = scrape_url
        info_hash_list = []
    elif isinstance(info_hash_hex, list):
        # Multiple hashes
        info_hash_list = []
        for hash_hex in info_hash_hex:
            try:
                hash_bytes = parse_info_hash(hash_hex, include_input=True)
                info_hash_list.append(hash_bytes)
            except ValueError as e:
                print(f"Error: Invalid info hash — {e}", file=sys.stderr)
                sys.exit(2)
        params_list = []
        for hash_bytes in info_hash_list:
            encoded = percent_encode_bytes(hash_bytes)
            params_list.append(f"info_hash={encoded}")

        query_string = '&'.join(params_list)
        separator = '&' if '?' in scrape_url else '?'
        full_url = f"{scrape_url}{separator}{query_string}"
    else:
        # Single hash
        try:
            info_hash_bytes = parse_info_hash(info_hash_hex)
            info_hash_list = [info_hash_bytes]
        except ValueError as e:
            print(f"Error: Invalid info hash — {e}", file=sys.stderr)
            sys.exit(2)
        encoded = percent_encode_bytes(info_hash_list[0])
        query_string = f"info_hash={encoded}"
        separator = '&' if '?' in scrape_url else '?'
        full_url = f"{scrape_url}{separator}{query_string}"

    # Only print headers for table format (not for json/csv)
    if output_format == 'table':
        print(f"\n{'─' * 50}")
        print(f"HTTP SCRAPE → {tracker_url}")
        print(f"{'─' * 50}")
        print(f"Client: {user_agent}")
        hash_count = len(info_hash_list)
        if hash_count == 0:
            print("Full scrape (no hash)")
        else:
            print(f"Scraping {hash_count} torrent{'s' if hash_count > 1 else ''}")
        print(f"Scrape URL: {full_url[:120]}{'...' if len(full_url) > 120 else ''}")

    req = urllib.request.Request(
        full_url,
        headers={
            'User-Agent': user_agent,
            'Accept-Encoding': accept_encoding,
        },
        method='GET'
    )

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            response_time_ms = (time.time() - start_time) * 1000
            status = resp.getcode()
            body = resp.read()

            content_encoding = resp.getheader('Content-Encoding', '')
            body = decode_http_body_for_content_encoding(body, content_encoding)
            response_encoding = normalize_response_encoding(content_encoding)

            # Only print status for table format
            if output_format == 'table':
                print(f"Status: {status}   Size: {len(body)} bytes   Response time: {response_time_ms:.2f}ms")

            if status != 200:
                if output_format == 'table':
                    print("Non-200 response — tracker likely dead or blocked")
                    if body:
                        print("Body preview:", body[:200].decode('ascii', errors='replace'))
                sys.exit(1)

            try:
                decoded = bdecode(body)
                if not isinstance(decoded, dict):
                    if output_format == 'table':
                        print("Response is not a bencoded dictionary")
                    sys.exit(1)

                # Extract failure reason if present (unofficial extension)
                failure_reason = decoded.get(b'failure reason', b'').decode('utf-8', errors='replace')
                if not failure_reason:
                    failure_reason = None

                # Extract flags dictionary (unofficial extension)
                flags = decoded.get(b'flags', {})
                min_request_interval = None
                if isinstance(flags, dict):
                    min_request_interval = flags.get(b'min_request_interval')

                # Extract files dictionary
                files = decoded.get(b'files', {})

                # Process each torrent in the scrape response
                torrents = []
                if isinstance(files, dict):
                    for info_hash_raw, stats in files.items():
                        if isinstance(stats, dict):
                            torrent_data = {
                                'info_hash': info_hash_raw.hex() if isinstance(info_hash_raw, bytes) else str(info_hash_raw),
                                'complete': stats.get(b'complete', 0),
                                'incomplete': stats.get(b'incomplete', 0),
                                'downloaded': stats.get(b'downloaded', 0),
                            }

                            # Extract optional name field
                            name = stats.get(b'name', b'')
                            if isinstance(name, bytes):
                                torrent_data['name'] = name.decode('utf-8', errors='replace')
                            elif name:
                                torrent_data['name'] = str(name)

                            torrents.append(torrent_data)

                data = {
                    'tracker': tracker_url,
                    'scrape_url': scrape_url,
                    'response_time_ms': round(response_time_ms, 2),
                    'response_encoding': response_encoding,
                    'failure_reason': failure_reason,
                    'min_request_interval': min_request_interval,
                    'torrents': torrents,
                    'torrent_count': len(torrents)
                }

                if output_format == 'json':
                    print(json.dumps(data, indent=2))
                elif output_format == 'csv':
                    format_scrape_csv_output(torrents)
                else:  # table
                    format_scrape_table_output(data, nocolor=nocolor)

                # Exit with error if there was a failure reason
                if failure_reason:
                    sys.exit(1)

            except Exception as e:
                if output_format == 'table':
                    print(f"Bdecode error: {str(e)}")
                    print("Raw preview (first 160 bytes):")
                    print(body[:160].hex(' ', -1))
                sys.exit(1)

    except urllib.error.HTTPError as e:
        if output_format == 'table':
            print(f"HTTP Error: {e.code} {e.reason}")
        sys.exit(1)
    except Exception as e:
        if output_format == 'table':
            print(f"Request failed: {type(e).__name__}: {str(e)}")
        sys.exit(1)

    # Return response time for batch mode tracking
    return round(response_time_ms, 2)

def parse_udp_url(tracker_url):
    """Parse UDP tracker URL and return (hostname, port)"""
    parsed = urllib.parse.urlparse(tracker_url)
    if parsed.scheme != 'udp':
        raise ValueError(f"Expected udp:// scheme, got {parsed.scheme}://")

    hostname = parsed.hostname
    port = parsed.port if parsed.port else 80

    if not hostname:
        raise ValueError("Invalid UDP tracker URL - no hostname")

    return hostname, port


def resolve_udp_socket_and_addr(hostname, port, output_format):
    """Resolve UDP endpoint and return (sock, addr, family)."""
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
        if not addr_info:
            if output_format == 'table':
                print(f"DNS resolution failed: No address found for {hostname}")
            sys.exit(1)
        family, _, _, _, sockaddr = addr_info[0]
        addr = sockaddr
        if output_format == 'table':
            print(f"Resolved to: {sockaddr[0]} ({'IPv6' if family == socket.AF_INET6 else 'IPv4'})")
    except socket.gaierror as e:
        if output_format == 'table':
            print(f"DNS resolution failed: {e}")
        sys.exit(1)

    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(DEFAULT_TIMEOUT)
    return sock, addr, family

def udp_connect(sock, addr, transaction_id):
    """Send UDP connect request and return connection_id"""
    # Connect request: protocol_id (8) + action (4) + transaction_id (4)
    request = struct.pack('!QII', UDP_PROTOCOL_ID, UDP_ACTION_CONNECT, transaction_id)

    sock.sendto(request, addr)

    try:
        response, _ = sock.recvfrom(16)
    except socket.timeout:
        raise TimeoutError("UDP connect request timed out")
    
    if len(response) < 16:
        raise ValueError(f"UDP connect response too short: {len(response)} bytes")
    
    # Response: action (4) + transaction_id (4) + connection_id (8)
    action, resp_transaction_id, connection_id = struct.unpack('!IIQ', response)

    if action != UDP_ACTION_CONNECT:
        raise ValueError(f"Expected action {UDP_ACTION_CONNECT}, got {action}")

    if resp_transaction_id != transaction_id:
        raise ValueError(f"Transaction ID mismatch: sent {transaction_id}, got {resp_transaction_id}")

    return connection_id

def udp_announce(sock, addr, connection_id, transaction_id, info_hash_bytes, event, peer_id, num_want, left=DEFAULT_LEFT):
    """Send UDP announce request and return parsed response"""
    # Map event string to UDP event codes
    event_map = {'started': 2, 'completed': 1, 'stopped': 3, 'none': 0}
    event_code = event_map.get(event, 0)

    # Announce request format:
    # connection_id (8) + action (4) + transaction_id (4) + info_hash (20) +
    # peer_id (20) + downloaded (8) + left (8) + uploaded (8) + event (4) +
    # ip (4) + key (4) + num_want (4) + port (2)

    request = struct.pack(
        '!QII20s20sQQQIIIIH',
        connection_id,           # connection_id
        UDP_ACTION_ANNOUNCE,     # action
        transaction_id,          # transaction_id
        info_hash_bytes,         # info_hash
        peer_id,                 # peer_id
        0,                       # downloaded
        left,                    # left
        0,                       # uploaded
        event_code,              # event
        0,                       # ip (0 = default)
        random.randint(0, 0xFFFFFFFF),  # key
        num_want,                # num_want
        6881                     # port
    )

    sock.sendto(request, addr)

    try:
        response, _ = sock.recvfrom(65536)
    except socket.timeout:
        raise TimeoutError("UDP announce request timed out")

    if len(response) < 20:
        raise ValueError(f"UDP announce response too short: {len(response)} bytes")

    # Response: action (4) + transaction_id (4) + interval (4) + leechers (4) + seeders (4) + peers (6*n)
    action, resp_transaction_id, interval, leechers, seeders = struct.unpack('!IIIII', response[:20])

    if action == 3:  # Error action
        error_msg = response[8:].decode('utf-8', errors='replace')
        raise ValueError(f"Tracker error: {error_msg}")

    if action != UDP_ACTION_ANNOUNCE:
        raise ValueError(f"Expected action {UDP_ACTION_ANNOUNCE}, got {action}")

    if resp_transaction_id != transaction_id:
        raise ValueError(f"Transaction ID mismatch: sent {transaction_id}, got {resp_transaction_id}")

    # Extract peer data (rest of response after header)
    peers_data = response[20:]

    return {
        'interval': interval,
        'leechers': leechers,
        'seeders': seeders,
        'peers_data': peers_data
    }

def udp_scrape(sock, addr, connection_id, transaction_id, info_hash_list):
    """Send UDP scrape request and return parsed torrent stats list."""
    if not info_hash_list:
        raise ValueError("UDP scrape requires at least one info_hash")

    request = struct.pack('!QII', connection_id, UDP_ACTION_SCRAPE, transaction_id)
    request += b''.join(info_hash_list)
    sock.sendto(request, addr)

    try:
        response, _ = sock.recvfrom(65536)
    except socket.timeout:
        raise TimeoutError("UDP scrape request timed out")

    if len(response) < 8:
        raise ValueError(f"UDP scrape response too short: {len(response)} bytes")

    action, resp_transaction_id = struct.unpack('!II', response[:8])

    if action == 3:  # Error action
        error_msg = response[8:].decode('utf-8', errors='replace')
        raise ValueError(f"Tracker error: {error_msg}")

    if action != UDP_ACTION_SCRAPE:
        raise ValueError(f"Expected action {UDP_ACTION_SCRAPE}, got {action}")

    if resp_transaction_id != transaction_id:
        raise ValueError(f"Transaction ID mismatch: sent {transaction_id}, got {resp_transaction_id}")

    expected_len = 8 + (12 * len(info_hash_list))
    if len(response) < expected_len:
        raise ValueError(
            f"UDP scrape response too short for {len(info_hash_list)} hash(es): "
            f"expected at least {expected_len} bytes, got {len(response)} bytes"
        )

    stats = []
    offset = 8
    for _ in info_hash_list:
        seeders, completed, leechers = struct.unpack('!III', response[offset:offset + 12])
        stats.append({
            'complete': seeders,
            'incomplete': leechers,
            'downloaded': completed,
        })
        offset += 12

    return stats

def test_udp_scrape(tracker_url, info_hash_hex, output_format, user_agent, nocolor=None):
    """Test UDP tracker scrape endpoint and return response time in milliseconds."""
    start_time = time.time()

    # UDP scrape requires explicit hash(es); full scrape is not part of BEP 15.
    if info_hash_hex is None:
        print("Error: Full scrape is not supported for UDP trackers. Provide --hash.", file=sys.stderr)
        sys.exit(2)

    # Normalize one or many hashes to bytes.
    if isinstance(info_hash_hex, list):
        hash_inputs = info_hash_hex
    else:
        hash_inputs = [info_hash_hex]

    info_hash_list = []
    for hash_hex in hash_inputs:
        try:
            hash_bytes = parse_info_hash(hash_hex, include_input=True)
            info_hash_list.append(hash_bytes)
        except ValueError as e:
            print(f"Error: Invalid info hash — {e}", file=sys.stderr)
            sys.exit(2)

    try:
        hostname, port = parse_udp_url(tracker_url)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    if output_format == 'table':
        print(f"\n{'─' * 50}")
        print(f"UDP SCRAPE → {tracker_url}")
        print(f"{'─' * 50}")
        print(f"Client: {user_agent}")
        print(f"Scraping {len(info_hash_list)} torrent{'s' if len(info_hash_list) > 1 else ''}")
        print(f"Connecting to: {hostname}:{port}")

    sock, addr, family = resolve_udp_socket_and_addr(hostname, port, output_format)

    try:
        # Step 1: Connect
        transaction_id = random.randint(0, 0xFFFFFFFF)
        if output_format == 'table':
            print(f"Sending connect request (transaction_id: {transaction_id})...")
        try:
            connection_id = udp_connect(sock, addr, transaction_id)
            if output_format == 'table':
                print(f"Connected (connection_id: {connection_id})")
        except (TimeoutError, ValueError) as e:
            if output_format == 'table':
                print(f"Connect failed: {e}")
            sys.exit(1)

        # Step 2: Scrape
        transaction_id = random.randint(0, 0xFFFFFFFF)
        if output_format == 'table':
            print(f"Sending scrape request (transaction_id: {transaction_id})...")
        try:
            scrape_stats = udp_scrape(sock, addr, connection_id, transaction_id, info_hash_list)
            response_time_ms = (time.time() - start_time) * 1000
        except (TimeoutError, ValueError) as e:
            if output_format == 'table':
                print(f"Scrape failed: {e}")
            sys.exit(1)

        if output_format == 'table':
            print(f"Scrape successful   Response time: {response_time_ms:.2f}ms")

        torrents = []
        for hash_bytes, stats in zip(info_hash_list, scrape_stats):
            torrents.append({
                'info_hash': hash_bytes.hex(),
                'complete': stats['complete'],
                'incomplete': stats['incomplete'],
                'downloaded': stats['downloaded'],
                'name': ''
            })

        data = {
            'tracker': tracker_url,
            'scrape_url': tracker_url,
            'response_time_ms': round(response_time_ms, 2),
            'response_encoding': None,
            'failure_reason': None,
            'min_request_interval': None,
            'torrents': torrents,
            'torrent_count': len(torrents)
        }

        if output_format == 'json':
            print(json.dumps(data, indent=2))
        elif output_format == 'csv':
            format_scrape_csv_output(torrents)
        else:  # table
            format_scrape_table_output(data, nocolor=nocolor)

    finally:
        sock.close()

    return round(response_time_ms, 2)

def test_udp_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, lookup_dns=False, left=DEFAULT_LEFT, nocolor=None):
    """Test UDP tracker and return response time in milliseconds"""
    start_time = time.time()

    try:
        info_hash_bytes = parse_info_hash(info_hash_hex)
    except ValueError as e:
        print(f"Error: Invalid info hash — {e}", file=sys.stderr)
        sys.exit(2)

    try:
        hostname, port = parse_udp_url(tracker_url)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    # Only print headers for table format
    if output_format == 'table':
        print(f"\n{'─' * 50}")
        print(f"UDP {event.upper()} → {tracker_url}")
        print(f"{'─' * 50}")
        print(f"Client: {user_agent}")
        print(f"Connecting to: {hostname}:{port}")

    # Resolve endpoint and determine IP version
    sock, addr, family = resolve_udp_socket_and_addr(hostname, port, output_format)
    is_ipv6 = family == socket.AF_INET6

    try:
        # Generate transaction ID
        transaction_id = random.randint(0, 0xFFFFFFFF)
        
        # Step 1: Connect
        if output_format == 'table':
            print(f"Sending connect request (transaction_id: {transaction_id})...")
        try:
            connection_id = udp_connect(sock, addr, transaction_id)
            if output_format == 'table':
                print(f"Connected (connection_id: {connection_id})")
        except (TimeoutError, ValueError) as e:
            if output_format == 'table':
                print(f"Connect failed: {e}")
            sys.exit(1)
        
        # Step 2: Announce
        transaction_id = random.randint(0, 0xFFFFFFFF)
        if output_format == 'table':
            print(f"Sending announce request (transaction_id: {transaction_id})...")
        try:
            announce_response = udp_announce(sock, addr, connection_id, transaction_id, info_hash_bytes, event, peer_id, num_want, left)
            response_time_ms = (time.time() - start_time) * 1000
        except (TimeoutError, ValueError) as e:
            if output_format == 'table':
                print(f"Announce failed: {e}")
            sys.exit(1)
        
        if output_format == 'table':
            print(f"Announce successful   Response time: {response_time_ms:.2f}ms")
        
        # Decode peers according to the address family we used
        peers_data = announce_response['peers_data']
        peer_list = []
        ipv4_peers = 0
        ipv6_peers = 0
        ipv4_bytes = 0
        ipv6_bytes = 0

        if len(peers_data) > 0:
            if is_ipv6:
                # Try IPv6 format first, fall back to IPv4 if needed
                if len(peers_data) % 18 == 0:
                    peer_list = decode_compact_peers_ipv6(peers_data)
                    ipv6_peers = len(peer_list)
                    ipv6_bytes = len(peers_data)
                elif len(peers_data) % 6 == 0:
                    if output_format == 'table':
                        print("Warning: Connected via IPv6 but received IPv4 peers response")
                    peer_list = decode_compact_peers_ipv4(peers_data)
                    ipv4_peers = len(peer_list)
                    ipv4_bytes = len(peers_data)
                else:
                    if output_format == 'table':
                        print(f"Warning: Unrecognized IPv6 peers data length: {len(peers_data)}")
            else:
                # IPv4 connection - should be IPv4 format
                if len(peers_data) % 6 == 0:
                    peer_list = decode_compact_peers_ipv4(peers_data)
                    ipv4_peers = len(peer_list)
                    ipv4_bytes = len(peers_data)
                else:
                    if output_format == 'table':
                        print(f"Warning: Unrecognized IPv4 peers data length: {len(peers_data)}")

        # Small debug output
        if output_format == 'table':
            if ipv6_peers > 0:
                # Detect IPv4-mapped IPv6 addresses (::ffff:x.x.x.x)
                # Only seen this behavior from: udp://tracker.theoks.net:6969/announce
                ipv4_mapped = sum(1 for p in peer_list if p.get('ip', '').startswith('::ffff:'))
                if ipv4_mapped > 0:
                    native_ipv6 = ipv6_peers - ipv4_mapped
                    print(f"  → Received {ipv6_peers} IPv6 peers ({ipv4_mapped} IPv4-mapped, {native_ipv6} native IPv6)")
                else:
                    print(f"  → Received {ipv6_peers} IPv6 peers")
            elif ipv4_peers > 0:
                print(f"  → Received {ipv4_peers} IPv4 peers")
            elif len(peers_data) > 0:
                print(f"  → Received {len(peers_data)} bytes of peers (format unknown)")

        total_peers_returned = len(peer_list)

        # Perform DNS lookup if requested
        if lookup_dns and show_peers and peer_list:
            apply_dns_lookup_to_peers(peer_list)

        data = {
            'tracker': tracker_url,
            'interval': announce_response['interval'],
            'min_interval': '?',  # UDP has no min_interval
            'seeds': announce_response['seeders'],
            'leechers': announce_response['leechers'],
            'downloaded': '?',
            'ipv4_peers': ipv4_peers,
            'ipv4_bytes': ipv4_bytes,
            'ipv6_peers': ipv6_peers,
            'ipv6_bytes': ipv6_bytes,
            'peer_list': peer_list,
            'num_want_requested': num_want,
            'total_peers_returned': total_peers_returned,
            'response_time_ms': round(response_time_ms, 2),
            'warning_message': None,  # UDP doesn't support warning messages
            'failure_reason': None,    # UDP doesn't support failure reasons (uses error action instead)
            'external_ip': None,       # UDP doesn't support external IP in standard protocol
            'tracker_id': None         # UDP doesn't support tracker ID
        }

        if output_format == 'json':
            format_json_output(data, show_peers, lookup_dns)
        elif output_format == 'csv':
            format_csv_output(data, show_peers, lookup_dns)
        else:  # table
            format_table_output(data, show_peers, lookup_dns, nocolor=nocolor)
        
    finally:
        sock.close()

    # Return response time for batch mode tracking
    return round(response_time_ms, 2)

# ────────────────────────────────────────────────
# Main dispatcher
# ────────────────────────────────────────────────

def test_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, scrape=False, lookup_dns=False, left=DEFAULT_LEFT, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None, bep34='prefer'):
    """Route to HTTP or UDP tracker based on URL scheme (returns (success, response_time) for batch mode)"""
    try:
        targets, _, err = _bep34_resolve_targets(tracker_url, bep34)
        if err and not targets:
            print(f"Error: {err}", file=sys.stderr)
            return False, None
        if err:
            print(f"Warning: {err}", file=sys.stderr)

        success, response_time, last_error = _attempt_tracker_targets(
            targets, info_hash_hex, event, output_format, show_peers,
            user_agent, peer_id, num_want, scrape, lookup_dns, left,
            accept_encoding, nocolor
        )
        if success:
            return True, response_time
        if _should_emit_attempt_error(last_error):
            print(f"Error: {last_error}", file=sys.stderr)
        return False, None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False, None

def _test_tracker_impl(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, scrape=False, lookup_dns=False, left=DEFAULT_LEFT, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None):
    """Internal implementation - routes to HTTP or UDP tracker based on URL scheme, returns response_time"""
    parsed = urllib.parse.urlparse(tracker_url)
    scheme = parsed.scheme.lower()
    
    if scheme in ('http', 'https'):
        if scrape:
            return test_http_scrape(
                tracker_url, info_hash_hex, output_format, user_agent,
                accept_encoding, nocolor=nocolor
            )
        else:
            return test_http_tracker(
                tracker_url, info_hash_hex, event, output_format, show_peers,
                user_agent, peer_id, num_want, lookup_dns, left, accept_encoding,
                nocolor=nocolor
            )
    elif scheme == 'udp':
        if scrape:
            return test_udp_scrape(tracker_url, info_hash_hex, output_format, user_agent, nocolor=nocolor)
        return test_udp_tracker(
            tracker_url, info_hash_hex, event, output_format, show_peers,
            user_agent, peer_id, num_want, lookup_dns, left, nocolor=nocolor
        )
    else:
        print(f"Error: Unsupported tracker scheme '{scheme}'. Only http, https, and udp are supported.", file=sys.stderr)
        sys.exit(2)

# ────────────────────────────────────────────────
# Batch mode functionality
# ────────────────────────────────────────────────

def batch_query_trackers(tracker_file, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, delay, random_qb, scrape=False, lookup_dns=False, left=DEFAULT_LEFT, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None, bep34='prefer'):
    """Query multiple trackers from a file"""
    colors = _color_palette(nocolor)
    BRIGHT_GREEN = colors['BRIGHT_GREEN']
    GREEN = colors['GREEN']
    YELLOW = colors['YELLOW']
    RED = colors['RED']
    BLUE = colors['BLUE']
    NC = colors['NC']

    # Read and count trackers
    try:
        with open(tracker_file, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"{RED}Error: Tracker file not found: {tracker_file}{NC}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"{RED}Error reading tracker file: {e}{NC}", file=sys.stderr)
        sys.exit(1)

    # Filter out comments and empty lines
    trackers = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):
            trackers.append(line)

    total = len(trackers)

    if total == 0:
        print(f"{RED}Error: No trackers found in {tracker_file}{NC}", file=sys.stderr)
        sys.exit(1)

    # Header
    print(f"{BLUE}{'=' * 40}{NC}")
    print(f"{BLUE}Batch Tracker {'Scrape' if scrape else 'Query'}{NC}")
    print(f"{BLUE}{'=' * 40}{NC}")
    print(f"Tracker file: {GREEN}{tracker_file}{NC}")
    print(f"Total trackers: {GREEN}{total}{NC}")
    if not scrape:
        print(f"Event: {GREEN}{event}{NC}")
        print(f"Show peers: {GREEN}{show_peers}{NC}")
        print(f"Num want: {GREEN}{num_want}{NC}")
    print(f"Delay: {GREEN}{delay}s{NC}")
    print(f"Info hash: {GREEN}{info_hash_hex if info_hash_hex else 'All torrents'}{NC}")
    print(f"{BLUE}{'=' * 40}{NC}\n")

    # Statistics
    success_count = 0
    failed_count = 0
    success_list = []
    failed_list = []
    response_times = []

    # Query each tracker
    for i, tracker in enumerate(trackers, 1):
        print(f"\n{BLUE}[{i}/{total}]{NC} {'Scraping' if scrape else 'Querying'} tracker...")
        print(f"{YELLOW}{tracker}{NC}")
        print("")

        # Get new random client for each query if --random-qb is enabled
        if random_qb:
            user_agent, peer_id = get_random_qb_client()

        # Query the tracker - use table format always in batch mode
        success, response_time = test_tracker(
            tracker, info_hash_hex, event, output_format, show_peers, user_agent,
            peer_id, num_want, scrape, lookup_dns, left, accept_encoding,
            nocolor=nocolor, bep34=bep34
        )

        if success:
            success_count += 1
            success_list.append((tracker, response_time))
            response_times.append(response_time)
            time_str = f" ({response_time:.2f}ms)" if response_time is not None else ""
            print(f"{GREEN}✓ Success{time_str}{NC}")
        else:
            failed_count += 1
            failed_list.append(tracker)
            print(f"{RED}✗ Failed{NC}")

        # Delay between requests (except after last one)
        if i < total and delay > 0:
            time.sleep(delay)

    # Summary
    print(f"\n{BLUE}{'=' * 40}{NC}")
    print(f"{BLUE}Summary{NC}")
    print(f"{BLUE}{'=' * 40}{NC}")
    print(f"Total trackers: {BLUE}{total}{NC}")
    print(f"Successful: {GREEN}{success_count}{NC}")
    print(f"Failed: {RED}{failed_count}{NC}")

    # Response time statistics
    if response_times:
        avg_time = sum(response_times) / len(response_times)
        min_time = min(response_times)
        max_time = max(response_times)
        print(f"\n{BLUE}Response Time Statistics:{NC}")
        print(f"  Average: {avg_time:.2f}ms")
        print(f"  Fastest: {min_time:.2f}ms")
        print(f"  Slowest: {max_time:.2f}ms")
    print(f"{BLUE}{'=' * 40}{NC}")

    # List successful trackers
    if success_count > 0:
        # Sort by response time (fastest first)
        success_list.sort(key=lambda x: x[1] if x[1] is not None else float('inf'))
        print(f"\n{GREEN}✓ Successful Trackers ({success_count}) - sorted by speed:{NC}")
        print(f"{GREEN}{'─' * 70}{NC}")
        for tracker, resp_time in success_list:
            if resp_time is not None:
                # Color code based on speed (4-tier system)
                time_color, _ = _response_time_color_and_label(resp_time, colors)
                print(f"  {time_color}{resp_time:>7.2f}ms{NC}  {tracker}")
            else:
                print(f"  {'    N/A':>10}  {tracker}")

    # List failed trackers
    if failed_count > 0:
        print(f"\n{RED}✗ Failed Trackers ({failed_count}):{NC}")
        print(f"{RED}{'─' * 40}{NC}")
        for tracker in failed_list:
            print(f"  {RED}•{NC} {tracker}")

    print("")

    # Exit with error if all failed
    if success_count == 0:
        sys.exit(1)
    sys.exit(0)

# ────────────────────────────────────────────────
# Retry Logic
# ────────────────────────────────────────────────

def test_tracker_with_retry(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, scrape, lookup_dns, max_attempts, left=DEFAULT_LEFT, accept_encoding=DEFAULT_ACCEPT_ENCODING, nocolor=None, bep34='prefer'):
    """
    Retry tracker connection until successful.

    Args:
        max_attempts: Maximum number of attempts. 0 means infinite retries.
        All other args: passed to test_tracker()

    Returns:
        (success, response_time) tuple from successful attempt, or (False, None) if max attempts reached
    """
    colors = _color_palette(nocolor)
    GREEN = colors['GREEN']
    YELLOW = colors['YELLOW']
    RED = colors['RED']
    BLUE = colors['BLUE']
    NC = colors['NC']

    attempt = 0
    retry_delay = 2  # seconds between retries
    # Resolve BEP34 targets once for the entire retry session to avoid DNS lookups
    # on every attempt.
    targets, _, bep34_err = _bep34_resolve_targets(tracker_url, bep34)
    if bep34_err and not targets:
        print(f"Error: {bep34_err}", file=sys.stderr)
        return False, None
    if bep34_err:
        print(f"Warning: {bep34_err}", file=sys.stderr)

    while True:
        attempt += 1

        # Show attempt number
        if max_attempts > 0:
            print(f"\n{BLUE}[Attempt {attempt}/{max_attempts}]{NC}")
        else:
            print(f"\n{BLUE}[Attempt {attempt}]{NC}")

        # Try to connect
        # Use resolved target list directly to avoid re-running BEP34 DNS on retries.
        success, response_time, last_error = _attempt_tracker_targets(
            targets, info_hash_hex, event, output_format, show_peers,
            user_agent, peer_id, num_want, scrape, lookup_dns, left,
            accept_encoding, nocolor
        )
        if not success and _should_emit_attempt_error(last_error):
            print(f"Error: {last_error}", file=sys.stderr)

        if success:
            print(f"\n{GREEN}✓ Connection successful after {attempt} attempt(s)!{NC}")
            return True, response_time

        # Check if we've reached max attempts
        if max_attempts > 0 and attempt >= max_attempts:
            print(f"\n{RED}✗ Failed after {max_attempts} attempt(s){NC}")
            return False, None

        # Wait before retry
        print(f"{YELLOW}Connection failed. Retrying in {retry_delay} seconds...{NC}")
        time.sleep(retry_delay)

# ────────────────────────────────────────────────
# Argument parsing
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query a BitTorrent tracker announce endpoint and display swarm info (seeds, leechers, peers). Supports HTTP/HTTPS and UDP trackers, as well as scrape requests.",
        formatter_class=lambda prog: argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=32),
        add_help=True
    )

    parser.add_argument(
        '-b', '--batch',
        action='store_true',
        help="Enable batch mode to query multiple trackers from a file (ignores --tracker)"
    )

    parser.add_argument(
        '-t', '--tracker',
        metavar='URL',
        default=DEFAULT_TRACKER,
        help="Tracker announce URL (http://, https://, or udp://). Ignored in batch mode."
    )

    parser.add_argument(
        '-H', '--hash',
        metavar='HEX',
        action='append',  # Allow multiple -H arguments
        help="Info hash (40 hex characters). Can be specified multiple times for scrape mode to query multiple torrents."
    )

    parser.add_argument(
        '-e', '--event',
        metavar='EVENT',
        choices=['started', 'completed', 'stopped', 'none'],
        default=DEFAULT_EVENT,
        help="Announce event type (choices: started, completed, stopped, none). Ignored in scrape mode."
    )

    parser.add_argument(
        '-o', '--format',
        metavar='FORMAT',
        choices=['table', 'json', 'csv'],
        default='table',
        help="Output format (choices: table, json, csv)."
    )

    parser.add_argument(
        '-f', '--file',
        metavar='FILE',
        default='trackers_to_query.txt',
        help="Tracker list file for batch mode (one tracker URL per line, # for comments)"
    )

    parser.add_argument(
        '-p', '--show-peers',
        action='store_true',
        help="Display the full list of peers (IP:port). Only applies to announce mode."
    )

    parser.add_argument(
        '-l', '--lookup',
        action='store_true',
        help="Perform reverse DNS lookup on peer IP addresses. Requires --show-peers. IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) are handled automatically."
    )

    parser.add_argument(
        '-r', '--random-qb',
        action='store_true',
        help="Use a random qBittorrent client version for the announce (spoofs User-Agent and peer_id)"
    )

    parser.add_argument(
        '-n', '--num-want',
        metavar='NUM',
        type=int,
        default=DEFAULT_NUM_WANT,
        help="Number of peers to request from tracker. Ignored in scrape mode."
    )

    parser.add_argument(
        '-L', '--left',
        metavar='BYTES',
        type=int,
        default=None,
        help=f"Bytes remaining to download. Defaults to 0 for --event completed, {DEFAULT_LEFT} otherwise. Use 0 to announce as a seeder."
    )

    parser.add_argument(
        '-d', '--delay',
        metavar='SECONDS',
        type=float,
        default=1.0,
        help="Delay between queries in batch mode (in seconds). Ignored in single-tracker mode."
    )

    parser.add_argument(
        '--nocolor',
        action='store_true',
        help="Disable colored output (useful for redirecting to files)"
    )

    parser.add_argument(
        '-a', '--accept-encoding',
        metavar='MODE',
        action='append',
        default=[],
        help="HTTP Accept-Encoding tokens (choices: all,gzip,deflate,br,zstd,identity). "
             "Repeat or comma-separate values (examples: -a br -a gzip, -a br,gzip). "
             "If 'all' is included, it overrides all other tokens. UDP unaffected."
    )

    parser.add_argument(
        '-s', '--scrape',
        action='store_true',
        help="Use scrape endpoint instead of announce. Supports HTTP/HTTPS and UDP trackers (UDP requires --hash; no full scrape)."
    )

    parser.add_argument(
        '--full-scrape',
        action='store_true',
        help="Scrape with no info_hash (implies --scrape). Tests if tracker allows full scrape."
    )

    parser.add_argument(
        '-R', '--retry',
        metavar='COUNT',
        nargs='?',
        const=0,
        type=int,
        help="Retry connection until successful. Specify COUNT for max attempts (e.g., --retry 5 or -R 5), or omit for infinite retries (e.g., --retry or -R). Only works in single-tracker mode."
    )
    parser.add_argument(
        '--bep34',
        choices=['off', 'prefer', 'strict'],
        default='prefer',
        help="BEP34 DNS TXT handling: off=disabled, prefer=try TXT-advertised endpoints first, strict=only allow TXT-advertised endpoints."
    )

    args = parser.parse_args()

    try:
        args.accept_encoding = build_accept_encoding_header(args.accept_encoding)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    # Set global NOCOLOR flag
    global NOCOLOR
    NOCOLOR = args.nocolor

    # Validate --retry only works in single-tracker mode
    if args.retry is not None and args.batch:
        print("Error: --retry only works in single-tracker mode, not in batch mode", file=sys.stderr)
        sys.exit(2)

    # Validate --lookup requires --show-peers
    if args.lookup and not args.show_peers:
        print("Error: --lookup requires --show-peers to be specified", file=sys.stderr)
        sys.exit(2)

    # --full-scrape implies --scrape with no hash
    if args.full_scrape:
        args.scrape = True
        info_hash = None  # No hash = full scrape
    # Handle hash argument - can be list (from append) or None
    elif args.hash is None or len(args.hash) == 0:
        # No hash provided - use default
        info_hash = DEFAULT_INFO_HASH_HEX
    elif len(args.hash) == 1:
        # Single hash provided
        info_hash = args.hash[0]
    else:
        # Multiple hashes provided (only valid for scrape)
        if not args.scrape:
            print("Error: Multiple --hash arguments are only supported in scrape mode", file=sys.stderr)
            sys.exit(2)
        info_hash = args.hash  # Keep as list for scrape

    # Resolve left value: explicit flag wins; otherwise smart default by event
    if args.left is not None:
        left = args.left
    elif args.event == 'completed':
        left = 0
    else:
        left = DEFAULT_LEFT

    # Determine client info
    if args.random_qb:
        user_agent, peer_id = get_random_qb_client()
    else:
        user_agent = DEFAULT_USER_AGENT
        peer_id = DEFAULT_PEER_ID

    # Run batch or single mode
    if args.batch:
        batch_query_trackers(
            args.file, info_hash, args.event, args.format, args.show_peers,
            user_agent, peer_id, args.num_want, args.delay, args.random_qb,
            args.scrape, args.lookup, left, args.accept_encoding,
            nocolor=args.nocolor, bep34=args.bep34
        )
    else:
        # Single tracker mode
        if args.retry is not None:
            # Retry mode: retry until success or max attempts
            max_attempts = args.retry if args.retry > 0 else 0  # 0 means infinite
            success, response_time = test_tracker_with_retry(
                args.tracker, info_hash, args.event, args.format, args.show_peers,
                user_agent, peer_id, args.num_want, args.scrape, args.lookup,
                max_attempts, left, args.accept_encoding, nocolor=args.nocolor, bep34=args.bep34
            )
        else:
            # Normal mode: single attempt
            success, response_time = test_tracker(
                args.tracker, info_hash, args.event, args.format, args.show_peers,
                user_agent, peer_id, args.num_want, args.scrape, args.lookup,
                left, args.accept_encoding, nocolor=args.nocolor, bep34=args.bep34
            )
        sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
