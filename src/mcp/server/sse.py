"""
SSE Server Transport Module

This module implements a Server-Sent Events (SSE) transport layer for MCP servers.

Example usage:
```
    # Create an SSE transport at an endpoint
    sse = SseServerTransport("/messages/")

    # Create Starlette routes for SSE and message handling
    routes = [
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ]

    # Define handler functions
    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(
                streams[0], streams[1], app.create_initialization_options()
            )
        # Return empty response to avoid NoneType error
        return Response()

    # Create and run Starlette app
    starlette_app = Starlette(routes=routes)
    uvicorn.run(starlette_app, host="127.0.0.1", port=port)
```

Note: The handle_sse function must return a Response to avoid a "TypeError: 'NoneType'
object is not callable" error when client disconnects. The example above returns
an empty Response() after the SSE connection ends to fix this.

See SseServerTransport class documentation for more details.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import ValidationError
from sse_starlette import EventSourceResponse
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

import mcp.types as types
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from mcp.shared.message import ServerMessageMetadata, SessionMessage

logger = logging.getLogger(__name__)


class SseServerTransport:
    """
    SSE server transport for MCP. This class provides _two_ ASGI applications,
    suitable to be used with a framework like Starlette and a server like Hypercorn:

        1. connect_sse() is an ASGI application which receives incoming GET requests,
           and sets up a new SSE stream to send server messages to the client.
        2. handle_post_message() is an ASGI application which receives incoming POST
           requests, which should contain client messages that link to a
           previously-established SSE session.
    """

    _endpoint: str
    _read_stream_writers: dict[UUID, MemoryObjectSendStream[SessionMessage | Exception]]
    _security: TransportSecurityMiddleware

    def __init__(self, endpoint: str, security_settings: TransportSecuritySettings | None = None) -> None:
        """
        Creates a new SSE server transport, which will direct the client to POST
        messages to the relative path given.

        Args:
            endpoint: A relative path where messages should be posted
                    (e.g., "/messages/").
            security_settings: Optional security settings for DNS rebinding protection.

        Note:
            We use relative paths instead of full URLs for several reasons:
            1. Security: Prevents cross-origin requests by ensuring clients only connect
               to the same origin they established the SSE connection with
            2. Flexibility: The server can be mounted at any path without needing to
               know its full URL
            3. Portability: The same endpoint configuration works across different
               environments (development, staging, production)

        Raises:
            ValueError: If the endpoint is a full URL instead of a relative path
        """

        super().__init__()

        # Validate that endpoint is a relative path and not a full URL
        if "://" in endpoint or endpoint.startswith("//") or "?" in endpoint or "#" in endpoint:
            raise ValueError(
                f"Given endpoint: {endpoint} is not a relative path (e.g., '/messages/'), "
                "expecting a relative path (e.g., '/messages/')."
            )

        # Ensure endpoint starts with a forward slash
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        self._endpoint = endpoint
        self._read_stream_writers = {}
        self._security = TransportSecurityMiddleware(security_settings)
        logger.debug(f"SseServerTransport initialized with endpoint: {endpoint}")

    @asynccontextmanager
    async def connect_sse(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            logger.error("connect_sse received non-HTTP request")
            raise ValueError("connect_sse can only handle HTTP requests")

        # Validate request headers for DNS rebinding protection
        request = Request(scope, receive)
        error_response = await self._security.validate_request(request, is_post=False)
        if error_response:
            await error_response(scope, receive, send)
            raise ValueError("Request validation failed")

        logger.debug("Setting up SSE connection")
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
        read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]

        write_stream: MemoryObjectSendStream[SessionMessage]
        write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        session_id = uuid4()
        self._read_stream_writers[session_id] = read_stream_writer
        logger.debug(f"Created new session with ID: {session_id}")

        # Determine the full path for the message endpoint to be sent to the client.
        # scope['root_path'] is the prefix where the current Starlette app
        # instance is mounted.
        # e.g., "" if top-level, or "/api_prefix" if mounted under "/api_prefix".
        root_path = scope.get("root_path", "")

        # self._endpoint is the path *within* this app, e.g., "/messages".
        # Concatenating them gives the full absolute path from the server root.
        # e.g., "" + "/messages" -> "/messages"
        # e.g., "/api_prefix" + "/messages" -> "/api_prefix/messages"
        full_message_path_for_client = root_path.rstrip("/") + self._endpoint

        # This is the URI (path + query) the client will use to POST messages.
        client_post_uri_data = f"{quote(full_message_path_for_client)}?session_id={session_id.hex}"

        sse_stream_writer, sse_stream_reader = anyio.create_memory_object_stream[dict[str, Any]](0)

        async def sse_writer():
            logger.debug("Starting SSE writer")
            async with sse_stream_writer, write_stream_reader:
                await sse_stream_writer.send({"event": "endpoint", "data": client_post_uri_data})
                logger.debug(f"Sent endpoint event: {client_post_uri_data}")

                async for session_message in write_stream_reader:
                    logger.debug(f"Sending message via SSE: {session_message}")
                    await sse_stream_writer.send(
                        {
                            "event": "message",
                            "data": session_message.message.model_dump_json(by_alias=True, exclude_none=True),
                        }
                    )

        async with anyio.create_task_group() as tg:

            async def response_wrapper(scope: Scope, receive: Receive, send: Send):
                """
                The EventSourceResponse returning signals a client close / disconnect.
                In this case we close our side of the streams to signal the client that
                the connection has been closed.
                """
                await EventSourceResponse(content=sse_stream_reader, data_sender_callable=sse_writer)(
                    scope, receive, send
                )
                await read_stream_writer.aclose()
                await write_stream_reader.aclose()
                logging.debug(f"Client session disconnected {session_id}")

            logger.debug("Starting SSE response task")
            tg.start_soon(response_wrapper, scope, receive, send)

            logger.debug("Yielding read and write streams")
            yield (read_stream, write_stream)

    async def handle_post_message(self, scope: Scope, receive: Receive, send: Send) -> None:
        logger.debug("Handling POST message")
        request = Request(scope, receive)

        # Validate request headers for DNS rebinding protection
        error_response = await self._security.validate_request(request, is_post=True)
        if error_response:
            return await error_response(scope, receive, send)

        session_id_param = request.query_params.get("session_id")
        if session_id_param is None:
            logger.warning("Received request without session_id")
            response = Response("session_id is required", status_code=400)
            return await response(scope, receive, send)

        try:
            session_id = UUID(hex=session_id_param)
            logger.debug(f"Parsed session ID: {session_id}")
        except ValueError:
            logger.warning(f"Received invalid session ID: {session_id_param}")
            response = Response("Invalid session ID", status_code=400)
            return await response(scope, receive, send)

        writer = self._read_stream_writers.get(session_id)
        if not writer:
            logger.warning(f"Could not find session for ID: {session_id}")
            response = Response("Could not find session", status_code=404)
            return await response(scope, receive, send)

        body = await request.body()
        logger.debug(f"Received JSON: {body}")

        try:
            message = types.JSONRPCMessage.model_validate_json(body)
            logger.debug(f"Validated client message: {message}")
        except ValidationError as err:
            logger.exception("Failed to parse message")
            response = Response("Could not parse message", status_code=400)
            await response(scope, receive, send)
            await writer.send(err)
            return

        # Pass the ASGI scope for framework-agnostic access to request data
        metadata = ServerMessageMetadata(request_context=request)
        session_message = SessionMessage(message, metadata=metadata)
        logger.debug(f"Sending session message to writer: {session_message}")
        response = Response("Accepted", status_code=202)
        await response(scope, receive, send)
        await writer.send(session_message)
