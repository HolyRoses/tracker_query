#!/usr/bin/env python3
"""
Tracker announce tester – Synology DSM compatible (no external modules)

Queries a BitTorrent tracker announce endpoint and shows
seeds / leechers / peer counts from the bencoded response.

Supports both HTTP/HTTPS and UDP trackers.

Examples:
  ./tracker_test.py --tracker http://open.acgtracker.com:1096/announce
  ./tracker_test.py -t udp://tracker.opentrackr.org:1337/announce
  ./tracker_test.py -t http://tracker.example.com/announce --event completed
  ./tracker_test.py --tracker udp://tracker2.com:6969 --hash deadbeef... --event started
  ./tracker_test.py -t http://tracker.example.com/announce --format json --show-peers
"""

import sys
import argparse
import urllib.parse
import urllib.request
import json
import socket
import struct
import random
import time
import os



# ────────────────────────────────────────────────
# Defaults
# ────────────────────────────────────────────────

# Global flag for color output (set by command-line argument)
NOCOLOR = False

DEFAULT_INFO_HASH_HEX = '5CB6C44712D494A87E8554839FB0541046B157AF'
DEFAULT_TRACKER       = 'udp://open.stealth.si:80/announce'
DEFAULT_PEER_ID       = b'-qB5140-' + os.urandom(12)
DEFAULT_USER_AGENT    = "qBittorrent/5.1.4"
DEFAULT_TIMEOUT       = 12
DEFAULT_EVENT         = 'started'
DEFAULT_NUM_WANT      = 50

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
        elif 48 <= b <= 57 or b == ord('-'):  # digit or negative for length
            colon = data.index(b':', i)
            length_str = data[i:colon].decode('ascii')
            length = int(length_str)
            start = colon + 1
            return data[start:start + length], start + length
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
        peers.append({'ip': ip, 'port': port, 'type': 'ipv6'})
    
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
# Output formatting (shared by HTTP and UDP)
# ────────────────────────────────────────────────

def format_table_output(data, show_peers=False):
    """Format data as a clean aligned table"""
    # Color codes for batch mode
    BRIGHT_GREEN = '\033[1;32m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    RED = '\033[0;31m'
    NC = '\033[0m'  # No Color
    
    # Skip colors if not in batch mode or if nocolor flag is set
    if not data.get('batch_mode') or NOCOLOR:
        BRIGHT_GREEN = GREEN = YELLOW = RED = NC = ''
    
    print("\nTracker Response Summary:")
    print("─" * 50)

    # Display response time with color coding
    response_time = data.get('response_time_ms')
    if response_time is not None:
        if response_time < 150:
            color = BRIGHT_GREEN
            speed = "Excellent"
        elif response_time < 300:
            color = GREEN
            speed = "Good"
        elif response_time < 500:
            color = YELLOW
            speed = "OK"
        else:
            color = RED
            speed = "Slow"
        print(f"Response Time:     {color}{response_time:>10.2f} ms ({speed}){NC}")
    else:
        print(f"Response Time:     {'N/A':>10}")

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
            print(f"{i:3d}. {peer['ip']:39s}:{peer['port']:<5d} [{peer['type']}]{peer_id_info}")
        print("─" * 50)

def format_json_output(data, show_peers=False):
    """Format data as JSON"""
    if not show_peers:
        # Remove peer_list from output if not requested
        data = {k: v for k, v in data.items() if k != 'peer_list'}
    print("\n" + json.dumps(data, indent=2))

def format_csv_output(data, show_peers=False):
    """Format data as CSV"""
    keys = ['response_time_ms', 'interval', 'min_interval', 'seeds', 'leechers', 'downloaded', 'ipv4_peers', 'ipv6_peers']
    print("\n" + ",".join(keys))
    print(",".join(str(data.get(k, '?')) for k in keys))
    
    if show_peers and data.get('peer_list'):
        print("\nip,port,type,peer_id")
        for peer in data['peer_list']:
            peer_id = peer.get('peer_id', '')
            print(f"{peer['ip']},{peer['port']},{peer['type']},{peer_id}")

# ────────────────────────────────────────────────
# HTTP Tracker Functions
# ────────────────────────────────────────────────

def build_announce_url(tracker_url, info_hash_bytes, event, peer_id, num_want):
    params = {
        'info_hash':   info_hash_bytes,
        'peer_id':     peer_id,
        'port':        '6881',
        'uploaded':    '0',
        'downloaded':  '0',
        'left':        '1000000000',
        'compact':     '1',
        'no_peer_id':  '1',
        'numwant':     str(num_want),
        'event':       event,
    }
    query = urllib.parse.urlencode(params, doseq=False, safe='~')
    return f"{tracker_url}?{query}"

def test_http_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want):
    """Test HTTP/HTTPS tracker and return response time in milliseconds"""
    start_time = time.time()

    try:
        info_hash_bytes = bytes.fromhex(info_hash_hex)
        if len(info_hash_bytes) != 20:
            raise ValueError("Info hash must be exactly 40 hex characters (20 bytes)")
    except ValueError as e:
        print(f"Error: Invalid info hash — {e}", file=sys.stderr)
        sys.exit(2)

    url = build_announce_url(tracker_url, info_hash_bytes, event, peer_id, num_want)
    print(f"\n{'─' * 50}")
    print(f"HTTP {event.upper()} → {tracker_url}")
    print(f"{'─' * 50}")
    print(f"Client: {user_agent}")
    print(f"URL: {url[:140]}{'...' if len(url) > 140 else ''}")

    req = urllib.request.Request(url, headers={'User-Agent': user_agent}, method='GET')

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            response_time_ms = (time.time() - start_time) * 1000
            status = resp.getcode()
            body = resp.read()
            print(f"Status: {status}   Size: {len(body)} bytes   Response time: {response_time_ms:.2f}ms")

            if status != 200:
                print("Non-200 response — tracker likely dead or blocked")
                if body:
                    print("Body preview:", body[:200].decode('ascii', errors='replace'))
                sys.exit(1)

            try:
                decoded = bdecode(body)
                if not isinstance(decoded, dict):
                    print("Response is not a bencoded dictionary")
                    sys.exit(1)

                failure = decoded.get(b'failure reason', b'').decode('utf-8', errors='replace')
                if failure:
                    print(f"Failure: {failure}")
                    sys.exit(1)

                interval     = decoded.get(b'interval',     '?')
                min_int      = decoded.get(b'min interval', '?')
                seeds        = decoded.get(b'complete',     0)
                leechers     = decoded.get(b'incomplete',   0)
                downloaded   = decoded.get(b'downloaded',   '?')

                peers_ipv4 = decoded.get(b'peers', b'')
                peers_ipv6 = decoded.get(b'peers6', b'')

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

                data = {
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
                    'response_time_ms': round(response_time_ms, 2)
                }

                if output_format == 'json':
                    format_json_output(data, show_peers)
                elif output_format == 'csv':
                    format_csv_output(data, show_peers)
                else:  # table
                    format_table_output(data, show_peers)

            except Exception as e:
                print(f"Bdecode error: {str(e)}")
                print("Raw preview (first 160 bytes):")
                print(body[:160].hex(' ', -1))
                sys.exit(1)

    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"Request failed: {type(e).__name__}: {str(e)}")
        sys.exit(1)

    # Return response time for batch mode tracking
    return round(response_time_ms, 2) if 'response_time_ms' in locals() else None

# ────────────────────────────────────────────────
# UDP Tracker Functions
# ────────────────────────────────────────────────

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

def udp_announce(sock, addr, connection_id, transaction_id, info_hash_bytes, event, peer_id, num_want):
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
        1000000000,              # left
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

def test_udp_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want):
    """Test UDP tracker and return response time in milliseconds"""
    start_time = time.time()

    try:
        info_hash_bytes = bytes.fromhex(info_hash_hex)
        if len(info_hash_bytes) != 20:
            raise ValueError("Info hash must be exactly 40 hex characters (20 bytes)")
    except ValueError as e:
        print(f"Error: Invalid info hash — {e}", file=sys.stderr)
        sys.exit(2)

    try:
        hostname, port = parse_udp_url(tracker_url)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"\n{'─' * 50}")
    print(f"UDP {event.upper()} → {tracker_url}")
    print(f"{'─' * 50}")
    print(f"Client: {user_agent}")
    print(f"Connecting to: {hostname}:{port}")

    # Resolve hostname to determine IP version
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
        if not addr_info:
            print(f"DNS resolution failed: No address found for {hostname}")
            sys.exit(1)

        # Use the first available address
        family, socktype, proto, canonname, sockaddr = addr_info[0]
        addr = sockaddr

        # Determine if IPv4 or IPv6
        is_ipv6 = family == socket.AF_INET6
        print(f"Resolved to: {sockaddr[0]} ({'IPv6' if is_ipv6 else 'IPv4'})")

    except socket.gaierror as e:
        print(f"DNS resolution failed: {e}")
        sys.exit(1)

    # Create UDP socket with appropriate family
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(DEFAULT_TIMEOUT)

    try:
        # Generate transaction ID
        transaction_id = random.randint(0, 0xFFFFFFFF)
        
        # Step 1: Connect
        print(f"Sending connect request (transaction_id: {transaction_id})...")
        try:
            connection_id = udp_connect(sock, addr, transaction_id)
            print(f"Connected (connection_id: {connection_id})")
        except (TimeoutError, ValueError) as e:
            print(f"Connect failed: {e}")
            sys.exit(1)
        
        # Step 2: Announce
        transaction_id = random.randint(0, 0xFFFFFFFF)
        print(f"Sending announce request (transaction_id: {transaction_id})...")
        try:
            announce_response = udp_announce(sock, addr, connection_id, transaction_id, info_hash_bytes, event, peer_id, num_want)
            response_time_ms = (time.time() - start_time) * 1000
        except (TimeoutError, ValueError) as e:
            print(f"Announce failed: {e}")
            sys.exit(1)
        
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
                # We announced over IPv6 → expect only IPv6 peers (18-byte stride)
                if len(peers_data) % 18 != 0:
                    print("Warning: peers data length not divisible by 18 (IPv6 expected)")
                peer_list = decode_compact_peers_ipv6(peers_data)
                ipv6_peers = len(peer_list)
                ipv6_bytes = len(peers_data)
            else:
                # Announced over IPv4 → expect only IPv4 peers (6-byte stride)
                if len(peers_data) % 6 != 0:
                    print("Warning: peers data length not divisible by 6 (IPv4 expected)")
                peer_list = decode_compact_peers_ipv4(peers_data)
                ipv4_peers = len(peer_list)
                ipv4_bytes = len(peers_data)

        # Small debug output
        if ipv6_peers > 0:
            print(f"  → Received {ipv6_peers} IPv6 peers")
        elif ipv4_peers > 0:
            print(f"  → Received {ipv4_peers} IPv4 peers")
        elif len(peers_data) > 0:
            print(f"  → Received {len(peers_data)} bytes of peers (format unknown)")

        total_peers_returned = len(peer_list)

        data = {
            'interval': announce_response['interval'],
            'min_interval': announce_response['interval'],  # UDP has no min_interval
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
            'response_time_ms': round(response_time_ms, 2)
        }

        if output_format == 'json':
            format_json_output(data, show_peers)
        elif output_format == 'csv':
            format_csv_output(data, show_peers)
        else:  # table
            format_table_output(data, show_peers)
        
    finally:
        sock.close()

    # Return response time for batch mode tracking
    return round(response_time_ms, 2) if 'response_time_ms' in locals() else None

# ────────────────────────────────────────────────
# Main dispatcher
# ────────────────────────────────────────────────

def test_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want):
    """Route to HTTP or UDP tracker based on URL scheme (returns (success, response_time) for batch mode)"""
    try:
        response_time = _test_tracker_impl(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want)
        return True, response_time
    except SystemExit as e:
        # Catch sys.exit() calls and convert to return value
        return (e.code == 0), None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False, None

def _test_tracker_impl(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want):
    """Internal implementation - routes to HTTP or UDP tracker based on URL scheme, returns response_time"""
    parsed = urllib.parse.urlparse(tracker_url)
    scheme = parsed.scheme.lower()
    
    if scheme in ('http', 'https'):
        return test_http_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want)
    elif scheme == 'udp':
        return test_udp_tracker(tracker_url, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want)
    else:
        print(f"Error: Unsupported tracker scheme '{scheme}'. Only http, https, and udp are supported.", file=sys.stderr)
        sys.exit(2)

# ────────────────────────────────────────────────
# Batch mode functionality
# ────────────────────────────────────────────────

def batch_query_trackers(tracker_file, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want, delay, random_qb):
    """Query multiple trackers from a file"""
    # Color codes
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'  # No Color

    # Disable colors if NOCOLOR flag is set
    if NOCOLOR:
        RED = GREEN = YELLOW = BLUE = NC = ''

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
    print(f"{BLUE}Batch Tracker Query{NC}")
    print(f"{BLUE}{'=' * 40}{NC}")
    print(f"Tracker file: {GREEN}{tracker_file}{NC}")
    print(f"Total trackers: {GREEN}{total}{NC}")
    print(f"Event: {GREEN}{event}{NC}")
    print(f"Show peers: {GREEN}{show_peers}{NC}")
    print(f"Num want: {GREEN}{num_want}{NC}")
    print(f"Delay: {GREEN}{delay}s{NC}")
    print(f"Info hash: {GREEN}{info_hash_hex}{NC}")
    print(f"{BLUE}{'=' * 40}{NC}\n")

    # Statistics
    success_count = 0
    failed_count = 0
    success_list = []
    failed_list = []
    response_times = []

    # Query each tracker
    for i, tracker in enumerate(trackers, 1):
        print(f"\n{BLUE}[{i}/{total}]{NC} Querying tracker...")
        print(f"{YELLOW}{tracker}{NC}")
        print("")

        # Get new random client for each query if --random-qb is enabled
        if random_qb:
            user_agent, peer_id = get_random_qb_client()

        # Query the tracker - use table format always in batch mode
        success, response_time = test_tracker(tracker, info_hash_hex, event, output_format, show_peers, user_agent, peer_id, num_want)

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
                if resp_time < 150:
                    time_color = '\033[1;32m'  # Bright Green (Excellent)
                elif resp_time < 300:
                    time_color = GREEN  # Green (Good)
                elif resp_time < 500:
                    time_color = YELLOW  # Yellow (OK)
                else:
                    time_color = RED  # Red (Slow)
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
# Argument parsing
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query a BitTorrent tracker announce endpoint and display swarm info (seeds, leechers, peers). Supports HTTP/HTTPS and UDP trackers.",
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
        default=DEFAULT_INFO_HASH_HEX,
        help="Info hash (40 hex characters)"
    )

    parser.add_argument(
        '-e', '--event',
        metavar='EVENT',
        choices=['started', 'completed', 'stopped', 'none'],
        default=DEFAULT_EVENT,
        help="Announce event type (choices: started, completed, stopped, none)"
    )

    parser.add_argument(
        '-o', '--format',
        metavar='FORMAT',
        choices=['table', 'json', 'csv'],
        default='table',
        help="Output format: table, json, or csv."
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
        help="Display the full list of peers (IP:port)"
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
        help="Number of peers to request from tracker"
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

    args = parser.parse_args()

    # Set global NOCOLOR flag
    global NOCOLOR
    NOCOLOR = args.nocolor

    # Determine client info
    if args.random_qb:
        user_agent, peer_id = get_random_qb_client()
    else:
        user_agent = DEFAULT_USER_AGENT
        peer_id = DEFAULT_PEER_ID

    # Run batch or single mode
    if args.batch:
        batch_query_trackers(args.file, args.hash, args.event, args.format, args.show_peers, user_agent, peer_id, args.num_want, args.delay, args.random_qb)
    else:
        # Single tracker mode
        test_tracker(args.tracker, args.hash, args.event, args.format, args.show_peers, user_agent, peer_id, args.num_want)

if __name__ == '__main__':
    main()
