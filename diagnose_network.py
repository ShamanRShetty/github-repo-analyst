"""
Run this script from your project root to diagnose GitHub API connectivity:
    python diagnose_network.py

It will tell you exactly what's failing and what timeout value to use.
"""
import asyncio
import httpx
import os
import socket
import subprocess
import sys
import time

GITHUB_API = "https://api.github.com"
TEST_REPO   = "python/cpython"


def _headers():
    token = os.getenv("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
        print(f"  GITHUB_TOKEN: set ({len(token)} chars)")
    else:
        print("  GITHUB_TOKEN: NOT SET — unauthenticated (60 req/hr limit)")
    return h


async def _try_get(label: str, url: str, headers: dict, connect: float, read: float):
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=connect, read=read, write=10.0, pool=5.0)
        ) as client:
            r = await client.get(url, headers=headers)
            elapsed = time.perf_counter() - t0
            print(f"  [{label}] {r.status_code} in {elapsed:.2f}s")
            return r.status_code, elapsed
    except httpx.ConnectTimeout:
        elapsed = time.perf_counter() - t0
        print(f"  [{label}] ConnectTimeout after {elapsed:.2f}s")
        return None, elapsed
    except httpx.ReadTimeout:
        elapsed = time.perf_counter() - t0
        print(f"  [{label}] ReadTimeout after {elapsed:.2f}s")
        return None, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"  [{label}] {type(e).__name__}: {e} after {elapsed:.2f}s")
        return None, elapsed


def check_dns():
    print("\n── 1. DNS resolution ─────────────────────────────────────")
    try:
        t0 = time.perf_counter()
        addr = socket.getaddrinfo("api.github.com", 443)
        print(f"  OK — resolved in {time.perf_counter()-t0:.2f}s → {addr[0][4][0]}")
        return True
    except socket.gaierror as e:
        print(f"  FAILED — DNS error: {e}")
        print("  → Check your internet connection or DNS settings")
        return False


def check_ping():
    print("\n── 2. Ping api.github.com ────────────────────────────────")
    try:
        result = subprocess.run(
            ["ping", "-n", "2", "-w", "3000", "api.github.com"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        for line in lines[-3:]:
            print(f"  {line}")
    except Exception as e:
        print(f"  Could not run ping: {e}")


async def check_connect():
    print("\n── 3. TCP connect (HTTPS port 443) ───────────────────────")
    headers = _headers()
    url = f"{GITHUB_API}/repos/{TEST_REPO}"

    # Try with escalating timeouts
    for connect_s in [5, 15, 30, 60]:
        status, elapsed = await _try_get(
            f"connect={connect_s}s", url, headers,
            connect=float(connect_s), read=30.0
        )
        if status is not None:
            print(f"\n  ✓ SUCCESS with connect={connect_s}s timeout")
            return connect_s, elapsed
        if elapsed < connect_s - 1:
            # Failed faster than the timeout — not a timeout issue
            print("  → Failed before timeout; likely a token or network block issue")
            break

    print("\n  ✗ All timeouts exhausted — api.github.com is not reachable")
    print("  Possible causes:")
    print("    • No internet / firewall / VPN blocking GitHub")
    print("    • Corporate proxy required (set HTTPS_PROXY env var)")
    print("    • Windows Defender or antivirus intercepting TLS")
    return None, None


def recommend(working_connect_s):
    print("\n── 4. Recommendation ─────────────────────────────────────")
    if working_connect_s is None:
        print("""
  GitHub API is unreachable from this machine. Nothing in mcp_tools.py
  can fix a network-level block. Try:

    a) Check if https://api.github.com is accessible in your browser
    b) If behind a corporate proxy, set:
           set HTTPS_PROXY=http://your-proxy:port
       then re-run this script
    c) If using a VPN, try toggling it off/on
    d) Temporarily disable Windows Defender Firewall and retry
""")
    else:
        # Add 10s safety margin
        recommended = working_connect_s + 10
        print(f"""
  Working connect timeout: {working_connect_s}s  →  set _TIMEOUT to {recommended}s

  In mcp_tools.py, change line:
    _TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)
  to:
    _TIMEOUT = httpx.Timeout(connect={recommended}.0, read=30.0, write=10.0, pool=5.0)
""")


async def main():
    print("GitHub API connectivity diagnostics")
    print("=" * 50)

    if not check_dns():
        recommend(None)
        return

    check_ping()
    working_s, _ = await check_connect()
    recommend(working_s)


if __name__ == "__main__":
    asyncio.run(main())