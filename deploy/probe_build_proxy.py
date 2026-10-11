"""Project-isolated HTTPS CONNECT to the authorized SOCKS5 gateway; no global proxy changes.

Only public package download hosts/443 are accepted. Do not publish this container port.
"""
import asyncio
import os
from urllib.parse import urlsplit

ALLOWED = ("github.com", "githubusercontent.com", "githubassets.com", "npmjs.org", "npmmirror.com", "debian.org")


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def handle(reader, writer):
    remote_writer = None
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        if len(header) > 8192:
            raise ValueError("header")
        method, target, _ = header.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
        url = urlsplit("https://" + target)
        host = url.hostname or ""
        if method != "CONNECT" or url.port != 443 or not any(host == s or host.endswith("." + s) for s in ALLOWED):
            raise ValueError("host")
        remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(
            os.environ["BUILD_SOCKS_HOST"], int(os.environ["BUILD_SOCKS_PORT"])), 10)
        remote_writer.write(b"\x05\x01\x00")
        await remote_writer.drain()
        if await remote_reader.readexactly(2) != b"\x05\x00":
            raise ValueError("socks")
        encoded = host.encode("idna")
        remote_writer.write(b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + b"\x01\xbb")
        await remote_writer.drain()
        reply = await remote_reader.readexactly(4)
        if reply[1] != 0:
            raise ValueError("socks connection")
        if reply[3] == 1:
            await remote_reader.readexactly(6)
        elif reply[3] == 4:
            await remote_reader.readexactly(18)
        elif reply[3] == 3:
            length = (await remote_reader.readexactly(1))[0]
            await remote_reader.readexactly(length + 2)
        else:
            raise ValueError("address")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        tasks = [asyncio.create_task(pipe(reader, remote_writer)), asyncio.create_task(pipe(remote_reader, writer))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        try:
            writer.write(b"HTTP/1.1 502 Connection Failed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()
        if remote_writer:
            remote_writer.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8888, limit=8192)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
