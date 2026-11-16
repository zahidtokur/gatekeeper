from unittest.mock import AsyncMock, Mock, patch

import httpx
from django.test import SimpleTestCase, override_settings


@override_settings(ALLOWED_HOSTS=["testserver"])
class UpstreamConnectionErrorTests(SimpleTestCase):
    """A connection error can't be produced by a running service, so it is mocked."""

    def setUp(self):
        self.upstream = Mock(spec=httpx.AsyncClient)
        self.upstream.request = AsyncMock()
        client_patch = patch("core.views.get_client", return_value=self.upstream)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    async def test_request_error_returns_502(self):
        for error_type in (httpx.ConnectError, httpx.RemoteProtocolError):
            with self.subTest(error_type=error_type.__name__):
                self.upstream.request.side_effect = error_type("connection failed")

                response = await self.async_client.get("/api/orders/1/")

                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "Upstream connection error"})
