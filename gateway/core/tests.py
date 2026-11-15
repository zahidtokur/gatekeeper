from unittest.mock import AsyncMock, Mock, patch

import httpx
from django.test import SimpleTestCase, override_settings

from core.views import GatewayProxy


@override_settings(ALLOWED_HOSTS=["testserver"])
class GatewayProxyTests(SimpleTestCase):
    def setUp(self):
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.get = AsyncMock(return_value=httpx.Response(
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
                self.upstream.get.reset_mock()

                response = await self.async_client.get(f"/api/{service}/{path}")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"id": 1})
                self.upstream.get.assert_awaited_once_with(target, params={})

    async def test_forwards_decoded_query_values(self):
        response = await self.async_client.get(
            "/api/users/1/?tag=first&tag=second&q=hello+world%21&empty=&page=2"
        )

        self.assertEqual(response.status_code, 200)
        self.upstream.get.assert_awaited_once_with(
            "http://users.test/v1/users/1/",
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
        self.assertEqual(response.json(), {"detail": "Service not found"})
        self.get_client.assert_not_called()
        self.upstream.get.assert_not_awaited()

    async def test_preserves_upstream_response(self):
        cases = (
            (201, b'{"created":true}', "application/json"),
            (404, b"missing", "text/plain; charset=utf-8"),
            (500, b"upstream failed", "text/plain"),
            (200, b"\x00\xff\x80", "application/octet-stream"),
        )
        for status, body, content_type in cases:
            with self.subTest(status=status, content_type=content_type):
                self.upstream.get.return_value = httpx.Response(
                    status, content=body, headers={"Content-Type": content_type}
                )

                response = await self.async_client.get("/api/users/1/")

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, body)
                self.assertEqual(response.headers["Content-Type"], content_type)

    async def test_timeout_returns_504(self):
        for error_type in (httpx.ConnectTimeout, httpx.ReadTimeout):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.get.side_effect = error_type("timed out")

                response = await self.async_client.get("/api/users/1/")

                self.assertEqual(response.status_code, 504)
                self.assertEqual(response.json(), {"detail": "Upstream timeout"})

    async def test_request_error_returns_502(self):
        for error_type in (httpx.ConnectError, httpx.RemoteProtocolError):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.get.side_effect = error_type("connection failed")

                response = await self.async_client.get("/api/orders/1/")

                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "Upstream connection error"})
