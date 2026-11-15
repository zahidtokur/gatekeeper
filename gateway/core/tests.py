import json
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from unittest.mock import AsyncMock, Mock, patch

import httpx
from django.test import SimpleTestCase, override_settings

from core import http as core_http
from core.views import GatewayProxy


@override_settings(ALLOWED_HOSTS=["testserver"])
class GatewayProxyTests(SimpleTestCase):
    def setUp(self):
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock(return_value=httpx.Response(
            200, json={"id": 1}
        ))
        client_patch = patch("core.views.get_client", return_value=self.upstream)
        self.get_client = client_patch.start()
        self.addCleanup(client_patch.stop)
        services_patch = patch.object(GatewayProxy, "services", {
            "users": "http://users.test/v1/users/",
            "orders": "http://orders.test/v1/orders/",
        })
        services_patch.start()
        self.addCleanup(services_patch.stop)

    async def test_routes_each_service_and_normalizes_trailing_slash(self):
        cases = (
            ("users", "", "http://users.test/v1/users/"),
            ("users", "1", "http://users.test/v1/users/1/"),
            ("users", "1/", "http://users.test/v1/users/1/"),
            ("orders", "", "http://orders.test/v1/orders/"),
            ("orders", "42", "http://orders.test/v1/orders/42/"),
        )
        for service, path, target in cases:
            with self.subTest(service=service, path=path):
                self.upstream.request.reset_mock()

                response = await self.async_client.get(f"/api/{service}/{path}")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"id": 1})
                self.upstream.request.assert_awaited_once_with(
                    "GET", target, content=None, headers={}, params={}
                )

    async def test_forwards_decoded_query_values(self):
        response = await self.async_client.get(
            "/api/users/1/?tag=first&tag=second&q=hello+world%21&empty=&page=2"
        )

        self.assertEqual(response.status_code, 200)
        self.upstream.request.assert_awaited_once_with(
            "GET",
            "http://users.test/v1/users/1/",
            content=None,
            headers={},
            params={
                "tag": ["first", "second"],
                "q": ["hello world!"],
                "empty": [""],
                "page": ["2"],
            },
        )

    async def test_unknown_service_returns_404_without_contacting_upstream(self):
        response = await self.async_client.get("/api/unknown/anything/")

        self.assertEqual(response.status_code, 404)
        self.get_client.assert_not_called()
        self.upstream.request.assert_not_awaited()

    async def test_preserves_upstream_response(self):
        cases = (
            (201, b'{"created":true}', "application/json"),
            (404, b"missing", "text/plain; charset=utf-8"),
            (500, b"upstream failed", "text/plain"),
            (200, b"\x00\xff\x80", "application/octet-stream"),
        )
        for status, body, content_type in cases:
            with self.subTest(status=status, content_type=content_type):
                self.upstream.request.return_value = httpx.Response(
                    status, content=body, headers={"Content-Type": content_type}
                )

                response = await self.async_client.get("/api/users/1/")

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, body)
                self.assertEqual(response.headers["Content-Type"], content_type)

    async def test_timeout_returns_504(self):
        for error_type in (httpx.ConnectTimeout, httpx.ReadTimeout):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.request.side_effect = error_type("timed out")

                response = await self.async_client.get("/api/users/1/")

                self.assertEqual(response.status_code, 504)
                self.assertEqual(response.json(), {"detail": "Upstream timeout"})

    async def test_request_error_returns_502(self):
        for error_type in (httpx.ConnectError, httpx.RemoteProtocolError):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.request.side_effect = error_type("connection failed")

                response = await self.async_client.get("/api/orders/1/")

                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "Upstream connection error"})

    async def test_post_forwards_json_body_content_type_and_query(self):
        body = b'{"items": [{"sku": "a", "qty": 2}], "meta": {"note": null}}'
        self.upstream.request.return_value = httpx.Response(201, json={"id": 7})

        response = await self.async_client.post(
            "/api/orders/?source=web&tag=a&tag=b",
            data=body,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"id": 7})
        self.upstream.request.assert_awaited_once_with(
            "POST",
            "http://orders.test/v1/orders/",
            content=body,
            headers={"content-type": "application/json"},
            params={"source": ["web"], "tag": ["a", "b"]},
        )

    async def test_post_does_not_forward_other_client_headers(self):
        await self.async_client.post(
            "/api/orders/",
            data=b"{}",
            content_type="application/json",
            headers={"Authorization": "Bearer secret", "X-Custom": "1"},
        )

        _, kwargs = self.upstream.request.call_args
        self.assertEqual(kwargs["headers"], {"content-type": "application/json"})

    async def test_gateway_keeps_serving_after_upstream_failures(self):
        self.upstream.request.side_effect = [
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
            httpx.Response(200, json={"id": 1}),
        ]

        first = await self.async_client.get("/api/users/1/")
        second = await self.async_client.get("/api/users/1/")
        third = await self.async_client.get("/api/users/1/")

        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 504)
        self.assertEqual(third.status_code, 200)
        self.assertEqual(third.json(), {"id": 1})
        self.assertEqual(self.upstream.request.await_count, 3)

    async def test_upstream_5xx_is_not_converted_to_gateway_error(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.upstream.request.return_value = httpx.Response(
                    status, json={"detail": "from upstream"}
                )

                response = await self.async_client.get("/api/users/1/")

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"detail": "from upstream"})


class _EchoHandler(BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.dumps({
            "method": self.command,
            "path": self.path,
            "content_type": self.headers.get("Content-Type"),
            "body": self.rfile.read(length).decode(),
        }).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *args):
        pass


@override_settings(ALLOWED_HOSTS=["testserver"])
class GatewaySmokeTests(SimpleTestCase):
    """Real HTTP round trip: gateway -> dummy upstream -> gateway."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingTCPServer(("127.0.0.1", 0), _EchoHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        core_http._client = None  # fresh client bound to this test's event loop
        services_patch = patch.object(GatewayProxy, "services", {
            "users": f"{self.base_url}/v1/users/",
        })
        services_patch.start()
        self.addCleanup(services_patch.stop)

    async def test_get_and_post_round_trip(self):
        response = await self.async_client.get("/api/users/1?q=a")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "/v1/users/1/?q=a")

        response = await self.async_client.post(
            "/api/users/", data=b'{"name": "x"}', content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["body"], '{"name": "x"}')
        self.assertEqual(response.json()["content_type"], "application/json")
        await core_http._client.aclose()
