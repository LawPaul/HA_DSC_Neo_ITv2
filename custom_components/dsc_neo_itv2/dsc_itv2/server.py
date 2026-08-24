"""asyncio TCP server wrapping :class:`SessionCodec`.

The TL280 communicator establishes an outbound TCP connection to our
listening port (default 3072) and starts the ITv2 handshake. This module
runs an :class:`asyncio.Server` that accepts those connections, pumps
bytes through a per-connection :class:`SessionCodec`, and exposes a
clean event-driven API for callers.

Usage::

    async with ITv2Server(settings, port=3072) as server:
        async for connection in server.connections():
            asyncio.create_task(handle(connection))

    async def handle(conn: ITv2Connection):
        async for event in conn.events():
            if isinstance(event, Connected):
                ...
            elif isinstance(event, Notification):
                ...
        # connection closed when the loop exits
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import inspect
import logging
from typing import Any

from .session import (
    BytesToSend,
    Closed,
    ConnectionSettings,
    Failed,
    SessionCodec,
)

_LOGGER = logging.getLogger(__name__)


SettingsProvider = Callable[..., Any]
"""``(peer_addr: tuple[str, int]) -> ConnectionSettings`` (sync or async).

Receives the panel's TCP peer address at connect time. Implementations
that don't care about per-panel routing can ignore the argument and
return a single fixed :class:`ConnectionSettings`.
"""

_READ_CHUNK = 4096
_SENTINEL_END: object = object()


class ITv2Connection:
    """One live ITv2 conversation with a single panel.

    The receive pump runs as a background task started by
    :class:`ITv2Server`. Callers consume events via :meth:`events` and
    push outbound messages via :meth:`send_message`.

    The connection auto-closes when the peer disconnects, when the codec
    enters a terminal state, or when :meth:`close` is awaited.
    """

    def __init__(
        self,
        codec: SessionCodec,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer_addr: tuple[str, int],
    ) -> None:
        self._codec = codec
        self._reader = reader
        self._writer = writer
        self._peer = peer_addr
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._pump_task: asyncio.Task | None = None

    @property
    def peer_addr(self) -> tuple[str, int]:
        return self._peer

    @property
    def session_id(self) -> str:
        return self._codec.session_id

    @property
    def is_open(self) -> bool:
        return not self._closed.is_set()

    async def events(self) -> AsyncIterator[Any]:
        """Yield events from the codec until the connection closes.

        :class:`BytesToSend` items are handled internally by the pump
        and are not surfaced. Callers see :class:`Connected`,
        :class:`Notification`, :class:`Closed`, :class:`Failed`.
        """
        while True:
            event = await self._events.get()
            if event is _SENTINEL_END:
                return
            yield event

    async def send_message(self, msg: Any) -> None:
        """Encode + transmit an application message.

        Caller must wait for :class:`Connected` before sending.
        """
        if not self.is_open:
            raise ConnectionError("connection is closed")
        wire = self._codec.send_message(msg)
        async with self._write_lock:
            self._writer.write(wire)
            await self._writer.drain()

    async def close(self) -> None:
        """Send Connection_End_Session, close socket, await pump exit."""
        if self._closed.is_set():
            return
        try:
            wire = self._codec.close()
            if wire:
                async with self._write_lock:
                    with contextlib.suppress(ConnectionError, OSError):
                        self._writer.write(wire)
                        await self._writer.drain()
        finally:
            await self._teardown()
            if self._pump_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump_task

    # ---- internal ----

    async def _run_pump(self) -> None:
        """Background task: read socket -> feed codec -> flush
        BytesToSend -> enqueue other events until the connection ends."""
        try:
            while True:
                try:
                    data = await self._reader.read(_READ_CHUNK)
                except (ConnectionError, OSError) as exc:
                    _LOGGER.debug("read error from %s: %s", self._peer, exc)
                    break
                if not data:
                    _LOGGER.debug("EOF from %s", self._peer)
                    break

                events = self._codec.feed(data)
                outbound = b"".join(
                    ev.data for ev in events if isinstance(ev, BytesToSend)
                )
                if outbound:
                    async with self._write_lock:
                        try:
                            self._writer.write(outbound)
                            await self._writer.drain()
                        except (ConnectionError, OSError) as exc:
                            _LOGGER.debug("write error to %s: %s", self._peer, exc)
                            break

                terminal = False
                for ev in events:
                    if isinstance(ev, BytesToSend):
                        continue
                    await self._events.put(ev)
                    if isinstance(ev, Closed | Failed):
                        terminal = True
                if terminal:
                    break
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        await self._events.put(_SENTINEL_END)
        try:
            self._writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await self._writer.wait_closed()
        except Exception:  # pragma: no cover (defensive)
            _LOGGER.debug("error closing writer for %s", self._peer, exc_info=True)


class ITv2Server:
    """asyncio TCP server that accepts ITv2 connections from one or
    more TL280 panels.

    Each accepted socket gets its own :class:`SessionCodec` (resolved
    via the supplied ``settings_provider``) and is wrapped in an
    :class:`ITv2Connection` that's emitted via :meth:`connections`.
    """

    def __init__(
        self,
        settings_provider: SettingsProvider | ConnectionSettings,
        host: str = "0.0.0.0",
        port: int = 3072,
        *,
        backlog: int = 8,
    ) -> None:
        if isinstance(settings_provider, ConnectionSettings):
            fixed = settings_provider
            self._settings_provider: SettingsProvider = lambda _peer: fixed
        else:
            self._settings_provider = settings_provider
        self._host = host
        self._port = port
        self._backlog = backlog
        self._server: asyncio.base_events.Server | None = None
        self._connection_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._connections: set[ITv2Connection] = set()
        self._stopped = asyncio.Event()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """Actual bound port. Reflects the OS-assigned port if started with ``port=0``."""
        if self._server is None:
            return self._port
        sockets = self._server.sockets or ()
        if not sockets:
            return self._port
        return sockets[0].getsockname()[1]

    @property
    def is_running(self) -> bool:
        return self._server is not None and not self._stopped.is_set()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("server already started")
        self._server = await asyncio.start_server(
            self._handle_client, host=self._host, port=self._port,
            backlog=self._backlog,
        )
        _LOGGER.info("ITv2 server listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        # Wake the connections() iterator
        await self._connection_queue.put(_SENTINEL_END)
        # Stop accepting new sockets
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        # Close all live connections
        live = list(self._connections)
        for conn in live:
            with contextlib.suppress(Exception):
                await conn.close()
        self._connections.clear()
        _LOGGER.info("ITv2 server stopped")

    async def connections(self) -> AsyncIterator[ITv2Connection]:
        """Yield each newly-accepted :class:`ITv2Connection` until stop()."""
        while True:
            item = await self._connection_queue.get()
            if item is _SENTINEL_END:
                return
            yield item

    async def __aenter__(self) -> ITv2Server:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    # ---- internal ----

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        peer_raw = writer.get_extra_info("peername")
        peer: tuple[str, int] = (
            (peer_raw[0], peer_raw[1]) if peer_raw else ("?", 0)
        )
        _LOGGER.debug("ITv2 connection from %s:%d", peer[0], peer[1])
        try:
            result = self._settings_provider(peer)
            if inspect.isawaitable(result):
                settings: ConnectionSettings = await result
            else:
                settings = result  # type: ignore[assignment]
        except Exception:
            _LOGGER.exception("settings_provider raised for %s", peer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        codec = SessionCodec(settings)
        conn = ITv2Connection(codec, reader, writer, peer)
        self._connections.add(conn)

        # Start the pump in the background.
        async def _run_and_cleanup() -> None:
            try:
                await conn._run_pump()
            finally:
                self._connections.discard(conn)

        conn._pump_task = asyncio.create_task(
            _run_and_cleanup(), name=f"itv2-pump-{peer[0]}:{peer[1]}",
        )

        # Hand the connection to the caller's iterator.
        await self._connection_queue.put(conn)
