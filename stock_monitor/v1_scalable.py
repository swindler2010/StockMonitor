"""Scalable Capital's official MCP transport. Requests are limited to portfolio reads."""
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
from threading import Lock
from urllib.parse import parse_qs, urlsplit
import webbrowser

from v1_portfolio import require_portfolio

URL = "https://mcp.scalable.capital/mcp"
PORT = 51241
CALLBACK = f"http://127.0.0.1:{PORT}/scalable/callback"
READS = {
    "list_accessible_portfolios",
    "get_portfolio_holdings",
    "get_portfolio_cash_breakdown",
    "get_portfolio_overview",
}
SYNC = Lock()
CACHE = "scalable-v1-snapshot.json"


class SyncError(ValueError):
    pass


class OAuthReturn:
    def __init__(self):
        self.server = None
        self.reply = None

    async def redirect(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".scalable.capital"):
            raise SyncError("SC_OAUTH_HOST")
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urlsplit(self.path)
                if path.path != "/scalable/callback":
                    self.send_error(404)
                    return
                args = parse_qs(path.query)
                owner.reply = (args.get("code", [None])[0], args.get("state", [None])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Return to Stock Monitor to see the connection result.")

        self.server = HTTPServer(("127.0.0.1", PORT), Callback)
        self.server.timeout = 1
        if not webbrowser.open(url):
            raise SyncError("SC_BROWSER_UNAVAILABLE")

    async def callback(self):
        if not self.server:
            raise SyncError("SC_AUTH_REQUIRED")
        deadline = asyncio.get_running_loop().time() + 150
        while self.reply is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.to_thread(self.server.handle_request)
        if not self.reply or not all(self.reply):
            raise SyncError("SC_AUTH_CANCELLED")
        return self.reply

    def close(self):
        if self.server:
            self.server.server_close()


class CredentialVault:
    """Use the Windows credential store for client metadata and OAuth tokens."""
    def __init__(self):
        from keyring.backends.Windows import WinVaultKeyring
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        self.vault = WinVaultKeyring()
        self.token_type = OAuthToken
        self.client_type = OAuthClientInformationFull

    def read(self, name, model):
        data = self.vault.get_password("Stock Monitor V1 Scalable Capital MCP", name)
        return model.model_validate_json(data) if data else None

    def write(self, name, model):
        self.vault.set_password("Stock Monitor V1 Scalable Capital MCP", name, model.model_dump_json())

    async def get_tokens(self):
        return self.read("tokens", self.token_type)

    async def set_tokens(self, tokens):
        self.write("tokens", tokens)

    async def get_client_info(self):
        return self.read("client", self.client_type)

    async def set_client_info(self, client_info):
        self.write("client", client_info)


async def read_tool(session, name, arguments=None):
    if name not in READS:
        raise SyncError("SC_READ_ONLY")
    result = await session.call_tool(name, arguments or {})
    payload = result.structuredContent
    if payload is None and len(result.content) == 1 and result.content[0].type == "text":
        try:
            payload = json.loads(result.content[0].text)
        except ValueError:
            raise SyncError("SC_RESPONSE_FORMAT") from None
    if result.isError or not isinstance(payload, dict) or payload.get("error"):
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        known = {"auth_required", "auth_invalid", "forbidden", "rate_limited", "upstream_unavailable"}
        raise SyncError(code if code in known else "SC_READ_FAILED")
    return payload


async def broker_snapshot(selected=None, interactive=False):
    try:
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientMetadata
        from pydantic import AnyUrl
        vault = CredentialVault()
    except ImportError:
        raise SyncError("SC_DEPENDENCIES_MISSING") from None

    callback = OAuthReturn()

    async def redirect(url):
        if not interactive:
            raise SyncError("SC_AUTH_REQUIRED")
        await callback.redirect(url)

    oauth = OAuthClientProvider(
        server_url=URL,
        client_metadata=OAuthClientMetadata(
            client_name="Stock Monitor V1",
            redirect_uris=[AnyUrl(CALLBACK)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=vault,
        redirect_handler=redirect,
        callback_handler=callback.callback,
    )
    try:
        async with asyncio.timeout(180 if interactive else 35):
            async with streamablehttp_client(URL, auth=oauth, terminate_on_close=False) as (receive, send, _):
                async with ClientSession(receive, send) as session:
                    await session.initialize()
                    available = await read_tool(session, "list_accessible_portfolios")
                    portfolios = [p for p in available.get("portfolios", []) if p.get("portfolioId")]
                    if not portfolios:
                        raise SyncError("SC_NO_PORTFOLIOS")
                    if selected not in {p["portfolioId"] for p in portfolios}:
                        if len(portfolios) != 1 or selected:
                            return {"selection": portfolios}
                        selected = portfolios[0]["portfolioId"]
                    args = {"portfolioId": selected}
                    holdings = await read_tool(session, "get_portfolio_holdings", args)
                    cash = await read_tool(session, "get_portfolio_cash_breakdown", args)
                    overview = await read_tool(session, "get_portfolio_overview", args)
                    require_portfolio(holdings, cash, overview, selected)
                    return dict(holdings=holdings, cash=cash, overview=overview,
                                synced_at=datetime.now(timezone.utc).isoformat())
    finally:
        callback.close()


def read_local(directory):
    path = Path(directory) / CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        require_portfolio(data["holdings"], data["cash"], data["overview"])
    except (OSError, TypeError, ValueError, KeyError):
        return None
    return data


def refresh(directory, selected=None, interactive=False):
    if not SYNC.acquire(blocking=False):
        raise SyncError("SC_SYNC_IN_PROGRESS")
    try:
        snapshot = asyncio.run(broker_snapshot(selected, interactive))
        if "selection" in snapshot:
            return snapshot
        path = Path(directory) / CACHE
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".scalable-v1-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return snapshot
    finally:
        SYNC.release()
