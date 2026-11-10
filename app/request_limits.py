"""Apply per-route body limits before parsing and avoid echoing rejected input."""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


class BoundedPayloadRoute(APIRoute):
    max_body_bytes: int
    body_limit_message: str
    no_store = False

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            size = 0
            too_large = HTTPException(413, self.body_limit_message)
            length = request.headers.get('content-length')

            async def receive():
                nonlocal size
                message = await request.receive()
                size += len(message.get('body', b''))
                if size > self.max_body_bytes:
                    raise too_large
                return message

            try:
                if length is not None:
                    try:
                        size_hint = int(length)
                    except ValueError:
                        raise HTTPException(400, 'Invalid Content-Length.') from None
                    if size_hint > self.max_body_bytes:
                        raise too_large
                response = await handler(Request(request.scope, receive))
            except RequestValidationError as error:
                details = [
                    {'loc': item['loc'][:-1] if item['type'] == 'extra_forbidden' else item['loc'],
                     'msg': item['msg'], 'type': item['type']}
                    for item in error.errors()[:12]
                ]
                response = JSONResponse(status_code=422, content={'detail': details})
            except HTTPException as error:
                if self.no_store:
                    error.headers = {**(error.headers or {}), 'Cache-Control': 'no-store'}
                raise
            if self.no_store:
                response.headers['Cache-Control'] = 'no-store'
            return response

        return bounded
