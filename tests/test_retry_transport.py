from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPMethod, HTTPStatus
from typing import TypeAlias
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import Request, Response
from httpx._types import URLTypes

from httpx_retry import retry_transport


class MockTransport(httpx.BaseTransport):
    def __init__(self, status_code_map: dict[URLTypes, Generator[tuple[int, str | None], None, None]] | None = None):
        self.status_code_map = status_code_map or {}

    def handle_request(self, request: Request) -> Response:
        # Simulate failure based on the URL
        status_code_generator = self.status_code_map.get(request.url)

        if status_code_generator is not None:
            status_code, retry_after_header = next(status_code_generator)
        else:
            status_code = 200
            retry_after_header = None
        return Response(
            status_code=status_code,
            request=request,
            headers={"Retry-After": retry_after_header} if retry_after_header else {},
        )


class MockAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, status_code_map: dict[URLTypes, AsyncGenerator[tuple[int, str | None], None]] | None = None
    ) -> None:
        self.status_code_map = status_code_map or {}

    async def handle_async_request(self, request: Request) -> Response:
        # Get the generator for the URL, if it exists
        status_code_generator = self.status_code_map.get(request.url)

        if status_code_generator is not None:
            # Get the next status code from the generator
            status_code, retry_after_header = await status_code_generator.__anext__()
        else:
            # If the URL is not in the map, return 200
            status_code = 200
            retry_after_header = None

        return Response(
            status_code=status_code,
            request=request,
            headers={"Retry-After": retry_after_header} if retry_after_header else {},
        )


def status_codes(codes: list[tuple[int, str | None]]) -> Generator[tuple[int, str | None], None, None]:
    """
    Yields the given status codes, and then the last status code indefinitely.
    """
    yield from codes
    while True:
        yield codes[-1]


async def astatus_codes(codes: list[tuple[int, str | None]]) -> AsyncGenerator[tuple[int, str | None], None]:
    """
    Yields the given status codes, and then the last status code indefinitely.
    """

    for code in codes:
        yield code

    while True:
        yield codes[-1]


MockTransportFixtureFunction: TypeAlias = Generator[tuple[Callable[..., MockTransport], MagicMock], None, None]
MockAsyncTransportFixtureFunction: TypeAlias = Generator[
    tuple[Callable[..., MockAsyncTransport], MagicMock], None, None
]
MockTransportFixture: TypeAlias = tuple[Callable[..., MockTransport], MagicMock]
MockAsyncTransportFixture: TypeAlias = tuple[Callable[..., MockAsyncTransport], MagicMock]


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch) -> MockTransportFixtureFunction:
    mock_sleep = MagicMock()
    monkeypatch.setattr("time.sleep", mock_sleep)

    def _mock_transport(
        status_code_map: dict[URLTypes, Generator[tuple[int, str | None], None, None]] | None = None,
    ) -> MockTransport:
        return MockTransport(status_code_map=status_code_map)

    yield _mock_transport, mock_sleep


@pytest.fixture
def mock_async_transport(monkeypatch: pytest.MonkeyPatch) -> MockAsyncTransportFixtureFunction:
    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    def _mock_async_transport(
        status_code_map: dict[URLTypes, AsyncGenerator[tuple[int, str | None], None]] | None = None,
    ) -> MockAsyncTransport:
        return MockAsyncTransport(status_code_map=status_code_map)

    yield _mock_async_transport, mock_sleep


def test_successful_request(mock_transport: MockTransportFixture) -> None:
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(get_transport())
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com")

    assert response.status_code == 200
    assert sleep_mock.call_count == 0


def test_failed_request(mock_transport: MockTransportFixture) -> None:
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(
        get_transport(status_code_map={"https://example.com/fail": status_codes([(429, None)])})
    )

    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")

    assert response.status_code == 429
    assert sleep_mock.call_count == 10


def test_unretryable_status_code(mock_transport: MockTransportFixture) -> None:
    status_code_map = {
        "https://example.com/fail": status_codes([(403, None), (200, None)]),
    }
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(get_transport(status_code_map=status_code_map))
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")
        assert response.status_code == 403

    assert sleep_mock.call_count == 0


def test_multiple_failures(mock_transport: MockTransportFixture) -> None:
    status_code_map = {
        "https://example.com/fail1": status_codes([(502, None), (200, None)]),
        "https://example.com/fail2": status_codes([(502, None)]),
    }
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(get_transport(status_code_map=status_code_map))
    with httpx.Client(transport=transport) as client:
        response1 = client.get("https://example.com/fail1")
        assert response1.status_code == 200

        response2 = client.get("https://example.com/fail2")

    assert response2.status_code == 502

    assert sleep_mock.call_count == 11


def test_custom_retryable_status_codes(mock_transport: MockTransportFixture) -> None:
    # Status code 500 (Internal Server Error) is not retryable by default
    # Status code 502 (Bad Gateway) is retryable by default; it won't be retried
    status_code_map = {
        "https://example.com/fail1": status_codes([(500, None), (200, None)]),
        "https://example.com/fail2": status_codes([(502, None), (200, None)]),
    }
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map), retry_status_codes=[HTTPStatus.INTERNAL_SERVER_ERROR]
    )
    with httpx.Client(transport=transport) as client:
        response1 = client.get("https://example.com/fail1")
        assert response1.status_code == 200

        response2 = client.get("https://example.com/fail2")
    assert response2.status_code == 502

    assert sleep_mock.call_count == 1


def test_custom_retryable_methods(mock_transport: MockTransportFixture) -> None:
    # Status code 502 (Bad Gateway) is retryable by default; it won't be retried
    status_code_map = {
        "https://example.com/fail": status_codes([(502, None), (200, None)]),
    }
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map), retryable_methods=[HTTPMethod.POST]
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")
        assert response.status_code == 502

    assert sleep_mock.call_count == 0


def test_custom_max_attempts(mock_transport: MockTransportFixture) -> None:
    status_code_map = {
        "https://example.com/fail": status_codes([(502, None)]),
    }
    get_transport, sleep_mock = mock_transport
    transport = retry_transport.RetryTransport(get_transport(status_code_map=status_code_map), max_attempts=5)
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")
        assert response.status_code == 502
        assert sleep_mock.call_count == 5


def test_backoff(mock_transport: MockTransportFixture) -> None:
    status_code_map = {
        "https://example.com/fail": status_codes([(502, None)]),
    }
    get_transport, sleep_mock = mock_transport

    backoff_factor = 2
    max_backoff_wait = 10

    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map),
        backoff_factor=backoff_factor,
        max_backoff_wait=10,
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")

    assert response.status_code == 502

    for attempt, (args, _) in enumerate(sleep_mock.call_args_list):
        this_sleep = args[0]

        # Exponential backoff means the successive waits between attempts will be be 2^attempt * backoff_factor
        # Full jitter means the actual value chosen will vary randomly between 0 and this value
        assert this_sleep <= 2**attempt * backoff_factor or this_sleep == max_backoff_wait


def test_invalid_retry_after(mock_transport: MockTransportFixture) -> None:
    status_code_map = {
        "https://example.com/fail": status_codes([(502, "invalid")]),
    }
    get_transport, sleep_mock = mock_transport

    backoff_factor = 2
    max_backoff_wait = 10

    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map),
        backoff_factor=backoff_factor,
        max_backoff_wait=10,
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")

    assert response.status_code == 502

    for attempt, (args, _) in enumerate(sleep_mock.call_args_list):
        this_sleep = args[0]

        # Behaviour should be the same as if no Retry-After header was present
        assert this_sleep <= 2**attempt * backoff_factor or this_sleep == max_backoff_wait


def test_retry_after_numeric(mock_transport: MockTransportFixture) -> None:
    status_code_map = {
        "https://example.com/fail": status_codes([(429, "5"), (429, "2"), (200, None)]),
    }
    get_transport, sleep_mock = mock_transport

    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map),
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")

    assert response.status_code == 200

    assert sleep_mock.call_args_list == [((5,),), ((2,),)]


def test_retry_after_http_date(mock_transport: MockTransportFixture) -> None:
    def imf_datetime(s: int) -> str:
        return (datetime.now(timezone(timedelta(hours=-5))) + timedelta(seconds=s)).strftime(
            "%a, %d %b %Y %H:%M:%S -0500"
        )

    status_code_map = {
        "https://example.com/fail": status_codes(
            [
                (429, imf_datetime(5)),
                (429, imf_datetime(30)),
                (200, None),
            ]
        ),
    }
    get_transport, sleep_mock = mock_transport

    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map),
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")

    assert response.status_code == 200

    for expected, (args, _) in zip([5, 30], sleep_mock.call_args_list):
        # Allow for execution time
        assert expected - 2 < args[0] < expected


def test_retry_after_http_date_no_tz(mock_transport: MockTransportFixture) -> None:
    def imf_datetime(s: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=s)).strftime("%a, %d %b %Y %H:%M:%S")

    status_code_map = {
        "https://example.com/fail": status_codes(
            [
                (429, imf_datetime(5)),
                (429, imf_datetime(30)),
                (200, None),
            ]
        ),
    }
    get_transport, sleep_mock = mock_transport

    transport = retry_transport.RetryTransport(
        get_transport(status_code_map=status_code_map),
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com/fail")

    assert response.status_code == 200

    for expected, (args, _) in zip([5, 30], sleep_mock.call_args_list):
        # Allow for execution time
        assert expected - 2 < args[0] < expected


@pytest.mark.asyncio
async def test_async_successful_request(mock_async_transport: MockAsyncTransportFixture) -> None:
    get_transport, sleep_mock = mock_async_transport
    transport = retry_transport.RetryTransport(get_transport())

    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.com")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_async_failed_request(mock_async_transport: MockAsyncTransportFixture) -> None:
    get_transport, sleep_mock = mock_async_transport
    transport = retry_transport.RetryTransport(
        get_transport(status_code_map={"https://example.com/fail": astatus_codes([(429, None)])})
    )

    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.com/fail")

    assert response.status_code == 429
    assert sleep_mock.call_count == 10


@pytest.mark.asyncio
async def test_async_custom_retryable_methods(mock_async_transport: MockAsyncTransportFixture) -> None:
    get_transport, sleep_mock = mock_async_transport
    transport = retry_transport.RetryTransport(
        get_transport(status_code_map={"https://example.com/fail": astatus_codes([(429, None)])}),
        retryable_methods=[HTTPMethod.POST],
    )

    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.com/fail")

    assert response.status_code == 429
    assert sleep_mock.call_count == 0
    assert sleep_mock.call_count == 0
