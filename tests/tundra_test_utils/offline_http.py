"""snowflake.core over a mock connection, with every request stopped at SFPoolManager._send.

The Root is real, so snowflake.core builds the URLs, quotes the path, adds showLimit,
serialises request bodies and deserialises the answers the way it does against
Snowflake. Nothing reaches urllib3: the request is recorded, its JSON body decoded, and
answered from `answers`, and a request with no answer fails.
"""

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import urllib3
from snowflake.connector import SnowflakeConnection
from snowflake.core import Root

from tundra_test_utils.offline import ROOT_INIT


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]]
    body: Any = None


@dataclass
class OfflineHttp:
    answers: Dict[Tuple[str, str], Tuple[int, Any]] = field(default_factory=dict)
    requests: List[Request] = field(default_factory=list)
    root: Optional[Root] = None

    def answer(self, path: str, body: Any, status: int = 200, method: str = "GET"):
        """
        Answer `method path` (as sent, percent-encoded) with `body`, JSON unless bytes.
        A `body` that's an exception is raised instead, as urllib3 raises a failure. A
        `body` that's callable is called as the request is sent and answers with what it
        returns, so a test can hold a request open as a stalled connection would.
        """
        self.answers[method, path] = (status, body)

    def install(self, monkeypatch) -> Root:
        """Allow a Root on a mock connection, stop its requests here, and build one."""
        from snowflake.core._http_requests import SFPoolManager

        capture = self

        def send(pool, method, url, fields, headers, urlopen_kw):
            return capture.send(method, url, urlopen_kw.get("body"))

        monkeypatch.setattr(Root, "__init__", ROOT_INIT)
        monkeypatch.setattr(SFPoolManager, "_send", send)
        # snowflake.core sets sys.tracebacklimit = 0 on every response it reads, which
        # would cut the tracebacks of every test after this one
        monkeypatch.setattr(sys, "tracebacklimit", None, raising=False)

        connection = mock.Mock(spec=SnowflakeConnection)
        connection.rest._host = "offline.invalid"
        connection.rest._protocol = "https"
        connection.rest._port = 443
        connection.rest.token = "session-token"
        connection.rest.master_token = "master-token"
        connection._telemetry = mock.Mock()
        self.root = Root(connection)
        return self.root

    def send(
        self, method: str, url: str, body: Optional[str] = None
    ) -> urllib3.HTTPResponse:
        parts = urlsplit(url)
        self.requests.append(
            Request(
                method,
                parts.path,
                parse_qs(parts.query),
                None if body is None else json.loads(body),
            )
        )
        if (method, parts.path) not in self.answers:
            raise AssertionError(f"No answer for {method} {url}")
        status, body = self.answers[method, parts.path]
        if callable(body):
            body = body()
        if isinstance(body, Exception):
            raise body
        return urllib3.HTTPResponse(
            body=body if isinstance(body, bytes) else json.dumps(body).encode(),
            status=status,
            headers={"Content-Type": "application/json"},
        )

    @property
    def last(self) -> Optional[Request]:
        return self.requests[-1] if self.requests else None
