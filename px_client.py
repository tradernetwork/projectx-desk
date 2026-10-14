"""ProjectX Gateway REST client. Read endpoints only; order writes live in px_guard.py.

Credentials come from PROJECTX_USERNAME / PROJECTX_API_KEY in the environment. The JWT never
leaves this object: it is not logged, not returned, and never appears in an exception message.
"""
import asyncio
import os
import time
from collections import deque

import httpx

def _default_api():
    return os.getenv("PROJECTX_API", "https://api.topstepx.com")   # read when a client is built, so .env.projectx applies

# Documented limits: retrieveBars 50 / 30s, everything else 200 / 60s. Stay under both.
GENERAL_LIMIT = (190, 60.0)
BARS_LIMIT = (45, 30.0)
BARS_PATH = "/api/History/retrieveBars"
# post() is for READS. Anything not on this list is refused at runtime, so a path built dynamically (split
# string, concatenation, config) still cannot reach an order-write endpoint through the retrying read path.
READ_PATHS = frozenset({
    "/api/Auth/loginKey", "/api/Account/search", "/api/Contract/search", "/api/Contract/searchById", BARS_PATH,
    "/api/Position/searchOpen", "/api/Order/searchOpen", "/api/Order/search", "/api/Trade/search",
})


class PXError(Exception):
    """Upstream failure. Message is safe to log and to show: no URL query, no token."""


class PXAuthError(PXError):
    pass


class PXNotConfigured(PXAuthError):
    """Credentials absent from the environment."""


class PXWriteUnknown(PXError):
    """A write may or may not have reached the broker. Never retry; the caller must reconcile."""


class PXRateLimited(PXError):
    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class _Window:
    """Sliding window limiter: wait() blocks until a call fits under n calls per `secs`."""

    def __init__(self, n, secs):
        self.n, self.secs, self.hits = n, secs, deque()

    async def wait(self):
        while True:
            now = time.monotonic()
            while self.hits and now - self.hits[0] >= self.secs:
                self.hits.popleft()
            if len(self.hits) < self.n:
                self.hits.append(now)
                return
            await asyncio.sleep(max(0.05, self.secs - (now - self.hits[0])))


class PXClient:
    def __init__(self, http=None, username=None, api_key=None, api=None):
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http is None
        self._user, self._key, self._api = username, api_key, api or _default_api()
        self._token = None
        self._login_lock = asyncio.Lock()
        self._general = _Window(*GENERAL_LIMIT)
        self._bars = _Window(*BARS_LIMIT)

    async def aclose(self):
        if self._owns_http:
            await self._http.aclose()

    async def token(self, fresh=False):
        """Current JWT. `fresh` forces a new login (used after a 401 or a hub auth failure)."""
        async with self._login_lock:
            if fresh:
                self._token = None
            if not self._token:
                user = self._user or os.environ.get("PROJECTX_USERNAME")
                key = self._key or os.environ.get("PROJECTX_API_KEY")
                if not user or not key:
                    raise PXNotConfigured("PROJECTX_USERNAME / PROJECTX_API_KEY not set")
                try:
                    r = await self._http.post(f"{self._api}/api/Auth/loginKey",
                                              json={"userName": user, "apiKey": key})
                    j = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    raise PXError(f"login failed: {type(e).__name__}") from None
                if not j.get("success") or not j.get("token"):
                    raise PXAuthError(f"login failed (HTTP {r.status_code})")      # gateway error text is never repeated
                self._token = j["token"]
            return self._token

    async def post(self, path, body, _retry=True):
        """POST a READ endpoint, return the decoded JSON. One re-login on 401. Writes use write() (guard only)."""
        if path not in READ_PATHS:
            raise PXError("post() only serves read endpoints; order writes go through PXClient.write")
        await (self._bars if path == BARS_PATH else self._general).wait()
        t = await self.token()
        try:
            r = await self._http.post(f"{self._api}{path}", json=body,
                                      headers={"Authorization": f"Bearer {t}"})
        except httpx.HTTPError as e:
            raise PXError(f"upstream {type(e).__name__}") from None
        if r.status_code == 401 and _retry:
            await self.token(fresh=True)
            return await self.post(path, body, _retry=False)
        if r.status_code == 429:
            raise PXRateLimited(r.headers.get("Retry-After", "5"))
        if r.status_code >= 400:
            raise PXError(f"upstream {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise PXError("upstream returned non-JSON") from None

    async def write(self, path, body):
        """POST for order writes. Exactly one attempt: no 401 re-login replay, no 429 retry, no limiter sleep.

        Only px_guard.py may call this (an AST test pins it). A transport failure after the request
        may have been sent raises PXWriteUnknown, which the guard turns into UNKNOWN + halt, because a
        replayed order is a duplicate position.
        """
        t = await self.token()  # failure here means nothing was sent
        try:
            r = await self._http.post(f"{self._api}{path}", json=body, headers={"Authorization": f"Bearer {t}"})
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise PXError("write not sent: connect failed") from None
        except httpx.HTTPError as e:
            raise PXWriteUnknown(f"write outcome unknown: {type(e).__name__}") from None
        if r.status_code == 429:
            raise PXRateLimited(r.headers.get("Retry-After", "5"))
        if r.status_code >= 400:
            raise PXWriteUnknown(f"write outcome unknown: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise PXWriteUnknown("write outcome unknown: non-JSON reply") from None

    # ---- read endpoints -------------------------------------------------------------------

    async def accounts(self, only_active=True):
        j = await self.post("/api/Account/search", {"onlyActiveAccounts": only_active})
        return j.get("accounts", [])

    async def search_contracts(self, text, live=False):
        j = await self.post("/api/Contract/search", {"live": live, "searchText": text})
        return j.get("contracts", [])

    async def contract_by_id(self, contract_id):
        j = await self.post("/api/Contract/searchById", {"contractId": contract_id})
        return j.get("contract")

    async def bars(self, body):
        j = await self.post(BARS_PATH, body)
        if not j.get("success"):
            raise PXError("bars request failed")      # gateway error text is never repeated
        return j.get("bars", [])

    async def open_positions(self, account_id):
        j = await self.post("/api/Position/searchOpen", {"accountId": account_id})
        return j.get("positions", [])

    async def open_orders(self, account_id):
        j = await self.post("/api/Order/searchOpen", {"accountId": account_id})
        return j.get("orders", [])

    async def search_orders(self, account_id, start, end):
        """All orders in a window, any status. Used to reconcile an UNKNOWN write by customTag."""
        j = await self.post("/api/Order/search", {"accountId": account_id,
                                                  "startTimestamp": start, "endTimestamp": end})
        return j.get("orders", [])

    async def trades(self, account_id, start, end):
        j = await self.post("/api/Trade/search", {"accountId": account_id,
                                                  "startTimestamp": start, "endTimestamp": end})
        return j.get("trades", [])
