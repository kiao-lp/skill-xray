"""Xray Cloud authentication, transport and GraphQL requests."""

import http.client
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


HOSTS = {
    "global": "xray.cloud.getxray.app",
    "us": "us.xray.cloud.getxray.app",
    "eu": "eu.xray.cloud.getxray.app",
    "au": "au.xray.cloud.getxray.app",
}
TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*")


class InputError(Exception):
    pass


class XrayError(Exception):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse_json(data):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON key")
        return result

    def invalid_constant(_):
        raise ValueError("JSON numbers must be finite")

    return json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def read_file(path):
    if not path.is_file():
        raise InputError(f"Expected a regular file at {path}")
    return path.read_bytes()


def read_text(path):
    try:
        return read_file(path).decode("utf-8")
    except UnicodeError:
        raise InputError(f"Expected UTF-8 text in {path}") from None


def read_json(path):
    try:
        value = parse_json(read_file(path))
        # Decode escaped surrogates and numeric overflow before constructing a request.
        json_text(value).encode("utf-8")
        return value
    except (ValueError, RecursionError):
        raise InputError(f"Invalid JSON in {path}") from None


def read_credentials():
    root = Path(os.environ.get("KIAO_WORKER_ROOT_PATH") or "/tmp")
    state = Path(os.environ.get("KIAO_WORKER_STATE_PATH") or root / ".kiao-config")
    path = state / "atlassian.json"
    if not state.is_absolute():
        raise InputError("Kiao worker state path must be absolute")
    config = read_json(path)
    xray = config.get("xray") if isinstance(config, dict) else None
    if not isinstance(xray, dict):
        raise InputError(f"Set the xray object in {path}")
    region = xray.get("region", "global")
    if not isinstance(region, str) or region not in HOSTS:
        raise InputError("xray.region must be global, us, eu or au")

    keys = {"client_id", "client_secret"} & xray.keys()
    if "token" in xray and not keys:
        names = ("token",)
    elif "token" not in xray and len(keys) == 2:
        names = ("client_id", "client_secret")
    else:
        raise InputError("Configure xray.token or the pair xray.client_id and xray.client_secret")
    credentials = {name: xray[name] for name in names}
    for name, value in credentials.items():
        if not isinstance(value, str) or not TOKEN.fullmatch(value):
            raise InputError(f"Invalid xray.{name} in {path}")
    return f"https://{HOSTS[region]}/api/v2", credentials


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class XrayClient:
    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.base = None
        self.token = None
        self.secrets = []

    def safe(self, value):
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [self.safe(item) for item in value]
        if isinstance(value, dict):
            return {self.safe(key): self.safe(item) for key, item in value.items()}
        return value

    def authenticate(self):
        self.base, credentials = read_credentials()
        self.secrets.extend(credentials.values())
        if "token" in credentials:
            self.token = credentials["token"]
            return
        token = self.json_request("POST", "/authenticate", payload=credentials, auth=False)
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise XrayError("Xray did not return a valid bearer token")
        self.token = token
        self.secrets.append(token)

    def request(self, method, path, *, data=None, content_type=None, query=None, auth=True, accept="application/json"):
        if auth and self.token is None:
            self.authenticate()
        url = self.base + path
        if query:
            url += "?" + urlencode(query)
        headers = {"Accept": accept}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            with exc:
                raise self.http_error(exc, auth) from None
        except (URLError, OSError, http.client.HTTPException):
            raise XrayError("Xray request failed while connecting or reading the response") from None

    def http_error(self, exc, auth):
        message = f"Xray returned HTTP {exc.code}"
        if 300 <= exc.code < 400:
            message += " (redirect refused; check xray.region)"
        elif exc.code == 401:
            message += " (check Xray credentials, token expiry and region)"
        elif exc.code == 403:
            message += " (check Xray permissions and region)"
        elif auth:
            try:
                data = exc.read(8192)
            except (OSError, http.client.HTTPException):
                data = b""
            detail = None
            try:
                body = json.loads(data)
            except ValueError:
                body = None
                if exc.headers.get("Content-Type", "").startswith("text/plain"):
                    detail = data.decode("utf-8", errors="replace").strip()
            if isinstance(body, dict):
                detail = body.get("message") or body.get("error")
                if not isinstance(detail, str):
                    details = body.get("errors")
                    detail = error_messages(details) if isinstance(details, list) else None
            if detail:
                message += ". " + self.safe(detail)[:400]
        retry_after = exc.headers.get("Retry-After") if exc.code in (429, 503) else None
        return XrayError(message, status=exc.code, retry_after=retry_after)

    def json_request(self, method, path, *, payload, auth=True):
        try:
            data = json_text(payload).encode("utf-8")
        except (ValueError, RecursionError):
            raise InputError("Request requires valid UTF-8 JSON") from None
        return decode_response(self.request(method, path, data=data, content_type="application/json", auth=auth))

    def graphql(self, document, variables):
        response = self.json_request("POST", "/graphql", payload={"query": document, "variables": variables})
        if isinstance(response, dict) and response.get("errors"):
            errors = response["errors"]
            detail = error_messages(errors) if isinstance(errors, list) else "request rejected"
            raise XrayError("Xray GraphQL error. " + self.safe(detail)[:400])
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise XrayError("Xray GraphQL returned no data")
        return response["data"]

    def field(self, name, args=None, selection="", *, mutation=False):
        document, variables = operation([(name, name, args or {}, selection)], mutation=mutation)
        return self.graphql(document, variables).get(name)


def error_messages(errors):
    messages = [item.get("message") if isinstance(item, dict) else item for item in errors[:3]]
    return " | ".join(item for item in messages if isinstance(item, str)) or "request rejected"


def decode_response(data):
    try:
        return parse_json(data)
    except (ValueError, RecursionError):
        raise XrayError("Xray did not return valid JSON") from None


def operation(fields, *, mutation=False):
    definitions, selections, variables = [], [], {}
    for alias, field, args, selection in fields:
        arguments = []
        for name, (type_name, value) in args.items():
            if value is not None:
                variable = f"{alias}_{name}"
                definitions.append(f"${variable}:{type_name}")
                arguments.append(f"{name}:${variable}")
                variables[variable] = value
        target = field if alias == field else f"{alias}:{field}"
        if arguments:
            target += "(" + ",".join(arguments) + ")"
        if selection:
            target += "{" + selection + "}"
        selections.append(target)
    prefix = "mutation" if mutation else "query"
    if definitions:
        prefix += "(" + ",".join(definitions) + ")"
    return prefix + "{" + " ".join(selections) + "}", variables
