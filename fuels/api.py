import json

from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError
from ninja.parser import Parser

from .views import _error, router


class StrictJSONParser(Parser):
    def parse_body(self, request):
        def reject_constant(value):
            raise ValueError('JSON numbers must be finite.')

        return json.loads(request.body, parse_constant=reject_constant)


api = NinjaAPI(
    title='Fuel Route API',
    version='1.0.0',
    description='Spotter Coding exam.',
    parser=StrictJSONParser(),
)


@api.exception_handler(ValidationError)
def invalid_request(request, error):
    # Preserve the existing 400 contract without echoing submitted values.
    messages = []
    for detail in error.errors:
        field = '.'.join(
            str(part) for part in detail['loc'] if part not in ('body', 'payload')
        )
        messages.append(f'{field}: {detail["msg"]}' if field else detail['msg'])
    return _error('invalid_request', '; '.join(messages), 400)


@api.exception_handler(HttpError)
def http_error(request, error):
    return _error('invalid_request', 'Cannot parse request body.', error.status_code)


api.add_router('/routes', router)
