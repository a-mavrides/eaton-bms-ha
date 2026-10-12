"""A local HTTPS simulator with the response shapes observed on firmware 8.00.34."""
from datetime import datetime, timedelta, timezone
import hashlib
import ssl

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
import pytest_asyncio

from custom_components.eaton_bms.api import BmsClient


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest_asyncio.fixture
async def panel(tmp_path, socket_enabled):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certfile, keyfile)
    zones = [{"id": str(i), "name": name, "state": "closed", "omittable": False, "omitted": False}
             for i, name in [(0, "Dining Area Right"), (1, "Living Room"), (2, "Dining Area Left"),
             (3, "Office"), (4, "Kitchen"), (50, "First Floor Corridor"), (51, "Bedroom 1"),
             (55, "Upstairs PIR"), (56, "Bedroom 2"), (57, "Playroom")]]
    result = {"requests": [], "zones": zones, "partition": {"id": "1", "state": "unset", "in-alarm": False},
              "deny": False, "command_status": 200, "streams": set(), "connected": 0,
              "faults": [], "alarms": [], "diagnostic_status": 200, "command_faults": []}

    async def route(request):
        auth = aiohttp.BasicAuth.decode(request.headers["Authorization"])
        result["requests"].append((request.method, request.path, auth.login, auth.password))
        if result["deny"] or auth.password != "test-password":
            return web.Response(status=401)
        if request.method == "PUT":
            if auth.login != "5678":
                return web.Response(status=401)
            data = await request.json()
            if result["command_status"] != 200:
                return web.json_response(result["command_faults"], status=result["command_status"])
            result["partition"]["state"] = data["state"]
            return web.json_response(result["partition"])
        assert auth.login == "1234"
        if request.path == "/system/":
            return web.json_response({"name": "Test House", "type": "i-on40H+", "partitions": ["1"]})
        if request.path == "/system/partitions/":
            return web.json_response([result["partition"]])
        if request.path == "/system/partitions-1/zones/":
            return web.json_response(result["zones"])
        if request.path == "/outputs/":
            return web.json_response([{"id": "0", "name": "Front Door", "state": "off"}])
        if request.path in ("/faults/", "/alarms/"):
            return web.json_response(result[request.path.strip("/")], status=result["diagnostic_status"])
        if request.path == "/events/":
            import asyncio
            queue = asyncio.Queue()
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            result["streams"].add(queue)
            result["connected"] += 1
            try:
                await response.write(b"event: heartbeat\ndata: ok\n\n")
                while True:
                    value = await queue.get()
                    if value is None:
                        break
                    # Deliberately split CRLF, UTF-8 and event delimiters across chunks.
                    body = value.encode()
                    for index in range(0, len(body), 7):
                        await response.write(body[index:index + 7])
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            finally:
                result["streams"].discard(queue)
            return response
        return web.Response(status=404)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", route)
    runner = web.AppRunner(app, shutdown_timeout=0)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await site.start()
    result["port"] = site._server.sockets[0].getsockname()[1]
    result["fingerprint"] = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    async def send(value):
        for queue in list(result["streams"]):
            await queue.put(value)

    result["send"] = send
    yield result
    await send(None)
    await runner.cleanup()


@pytest_asyncio.fixture
async def client(panel):
    async with aiohttp.ClientSession() as session:
        yield BmsClient(session, "127.0.0.1", panel["port"], "1234", "test-password", panel["fingerprint"])
