# -*- coding: utf-8 -*-

import base64
import hashlib
import hmac
import json
import logging

import requests
from requests.auth import AuthBase
from requests.compat import urlparse, urlsplit
from requests.utils import quote, unquote

from .utils import remove_trailing_slash, show_response_html

logger = logging.getLogger(__name__)


REQUEST_TIMEOUT = (10, 10)  # request and read timeout, respectively, in seconds


class HmacAuth(AuthBase):
    """
    Implements Hash-based Message Authentication Codes (HMAC). HMAC guarantees that: A) a message
    has been generated by a holder of the secret key, and B) that its contents haven't been
    altered since the auth code was generated.
    Instances of HmacAuth are immutable and are therefore safe to use in multiple threads.
    :param username the username of the ice user who ICE activity will be attributed to.
    Overrides the value provided by user_auth if both are present. At least one is required.
    :raises ValueError if no user email address is provided.
    """

    KEYSTORE = {}

    @classmethod
    def deregister_key(cls, key_id):
        del cls.KEYSTORE[key_id]

    @classmethod
    def register_key(cls, key_id, secret_key):
        cls.KEYSTORE[key_id] = secret_key

    def __init__(self, key_id, username=None):
        """
        :param key_id: identifier of the key registered with HmacAuth
        :param username: the ID of the user to send to the remote service
        """
        secret_key = self.KEYSTORE.get(key_id, None)
        if not secret_key:
            raise ValueError("A secret key is required input for HMAC authentication")
        self._KEY_ID = key_id
        self._USERNAME = username
        self._SECRET_KEY = secret_key

    def __call__(self, request):
        """
        Overrides the empty base implementation to provide authentication for the provided request
        object.
        """

        # generate a signature for the message by hashing the request using the secret key
        sig = self._build_signature(request)

        # add message headers including the username (if present) and message
        # The version 1 spec of the HmacSignature class calls for the Authorization HTTP header
        #   of the form: {Version}:{KeyId}:{UserId}:{Signature}
        header = ":".join(("1", self._KEY_ID, self._USERNAME, sig))
        request.headers["Authorization"] = header
        return request

    def _build_message(self, request):
        """
        Builds a string representation of the message contained in the request so it can be
        digested for HMAC generation
        """
        url = urlparse(request.url)

        # THe version 1 spec of the HmacSignature class calls for the message to be signed
        #   formatted as the following elements, each separated by a newline character:
        #   * UserId (same value as used in Authorization header)
        #   * HTTP Method (e.g. GET, POST)
        #   * HTTP Host (e.g. server.example.org)
        #   * Request path (e.g. /path/to/resource/)
        #   * SORTED query string, keyed by natural UTF8 byte-ordering of names
        #   * Request Body
        delimiter = "\n"
        body = ""
        if request.body:
            # Django request object has body as bytes; requests request object has body as str
            if isinstance(request.body, bytes):
                body = request.body.decode("utf-8")
            else:
                body = request.body
        msg = delimiter.join(
            (
                self._USERNAME or "",
                request.method,
                url.netloc,
                url.path,
                self._sort_parameters(url.query),
                body,
            )
        )
        return msg.encode("utf-8")

    def _build_signature(self, request):
        """
        Builds a signature for the provided request message based on the secret key.
        """
        key = base64.b64decode(self._SECRET_KEY)
        msg = self._build_message(request)
        digest = hmac.new(key, msg=msg, digestmod=hashlib.sha1).digest()
        sig = base64.b64encode(digest).decode()
        return sig

    def _sort_parameters(self, query):
        # split on ampersand
        params = query.split("&")
        # split each param into two-item lists of (key,value) and quote list entries
        params = [[quote(unquote(v)) for v in item.split("=", 1)] for item in params]
        # sort based on key portion
        params = sorted(params, key=lambda p: p[0])
        # join back together on ampersand
        return "&".join(map(lambda p: "=".join(p), params))


# ICE's current automatic limit on results returned in the absence of a specific requested
# page size
DEFAULT_RESULT_LIMIT = 15
DEFAULT_PAGE_NUMBER = 1
RESULT_LIMIT_PARAMETER = "limit"
_JSON_CONTENT_TYPE_HEADER = {"Content-Type": "application/json; charset=utf8"}


class IceSessionAuth(AuthBase):
    """
    Implements session-based authentication for ICE. At the time of initial implementation,
    "session-based" is a bit misleading for the processing performed here, since ICE's login
    mechanism doesn't reply with set-cookie headers or read the session ID in the session cookie.
    Instead, ICE's REST API responds to a successful login with a JSON object containing the
    session ID, and authenticates subsequent requests by requiring the session ID in each
    subsequent request header.

    Clients should first call login() to get a valid ice session id
    """

    def __init__(self, session_id):
        self._session_id = session_id

    def __call__(self, request):
        """
        Sets the request header X-ICE-Authentication-SessionId with the ICE session ID.
        """
        request.headers["X-ICE-Authentication-SessionId"] = self._session_id
        return request

    @staticmethod
    def login(
        base_url,
        username,
        password,
        user_auth=None,
        timeout=REQUEST_TIMEOUT,
        verify_ssl_cert=True,
    ):
        """
        Logs into ICE at the provided base URL or raises an Exception if an unexpected response is
        received from the server.
        :param base_url: the base URL of the ICE installation (not including the protocol
        :param timeout a tuple representing the connection and read timeouts, respectively, in
        seconds, for the login request to ICE's REST API
        :param verify_ssl_cert True to verify ICE's SSL certificate. Provided so clients can ignore
        self-signed certificates during *local* EDD / ICE testing on a single development machine.
        Note that it's very dangerous to skip certificate verification when communicating across
        the network, and this should NEVER be done in production.
        :return: new SessionAuth containing the newly-created session. Note that at present the
        session isn't strictly required, but is provided for completeness in case ICE's
        behavior changes to store the session ID as a cookie instead of requiring it as a request
        header.
        """

        if not username:
            username = user_auth.email if user_auth else None

        if not username:
            raise ValueError("At least one source of ICE username is required")

        # chop off the trailing '/', if any, so we can write easier-to-read URL snippets in our
        # code (starting w '%s/'). also makes our code trailing-slash agnostic.
        base_url = remove_trailing_slash(base_url)

        # build request parameters for login
        login_dict = {"email": username, "password": password}
        login_resource_url = "%(base_url)s/rest/accesstokens/" % {"base_url": base_url}

        # issue a POST to request login from the ICE REST API
        response = requests.post(
            login_resource_url,
            headers=_JSON_CONTENT_TYPE_HEADER,
            data=json.dumps(login_dict),
            timeout=timeout,
            verify=verify_ssl_cert,
        )

        # raise an exception if the server didn't give the expected response
        if response.status_code != requests.codes.ok:
            response.raise_for_status()

        json_response = response.json()
        session_id = json_response["sessionId"]

        # if login failed for any other reason,
        if not session_id:
            raise ValueError(
                "Server responded successfully, but response did not include the "
                "required session id"
            )

        logger.info("Successfully logged into ICE at %s" % base_url)

        return IceSessionAuth(session_id)


DJANGO_CSRF_COOKIE_KEY = "csrftoken"


def insert_spoofed_https_csrf_headers(headers, base_url):
    """
    Creates HTTP headers that help to work around Django's CSRF protection, which shouldn't apply
    outside of the browser context.
    :param headers: a dictionary into which headers will be inserted, if needed
    :param base_url: the base URL of the Django application being contacted
    """
    # if connecting to Django/DRF via HTTPS, spoof the 'Host' and 'Referer' headers that Django
    # uses to help prevent cross-site scripting attacks for secure browser connections. This
    # should be OK for a standalone Python REST API client, since the origin of a
    # cross-site scripting attack is malicious website code that executes in a browser,
    # but accesses another site's credentials via the browser or via user prompts within the
    # browser. Not applicable in this case for a standalone REST API client.
    # References:
    # https://docs.djangoproject.com/en/dev/ref/csrf/#how-it-works
    # http://security.stackexchange.com/questions/96114/why-is-referer-checking-needed-for-django
    # http://mathieu.fenniak.net/is-your-web-api-susceptible-to-a-csrf-exploit/
    # -to-prevent-csrf
    if urlparse(base_url).scheme == "https":
        headers["Host"] = urlsplit(base_url).netloc
        headers["Referer"] = base_url  # LOL! Bad spelling is now standard :-)


class EddSessionAuth(AuthBase):
    """
    Implements session-based authentication for EDD.
    """

    SESSION_ID_KEY = "sessionid"

    def __init__(self, cookie_jar, csrftoken):
        self.cookie_jar = cookie_jar
        self.csrftoken = csrftoken

    def __call__(self, request):
        """
        Sets the Django CSRF token in headers.
        """
        self.prev_request = request  # TODO: for debugging, remove
        if request.body:
            # the CSRF token can change; pull the correct value out of cookie
            try:
                jar = request.headers["Cookie"]
                label = "csrftoken="
                offset = jar.find(label) + len(label)
                end = jar.find(";", offset)
                request.headers["X-CSRFToken"] = jar[offset:end]
            except Exception:
                # ignore any problems loading token from cookie
                pass
        return request

    def apply_session_token(self, session_obj):
        session_obj.cookies.update(self.cookie_jar)

    @staticmethod
    def login(
        username,
        password,
        base_url="https://edd.jbei.org",
        timeout=REQUEST_TIMEOUT,
        verify_ssl_cert=True,
        debug=False,
    ):
        """
        Logs into EDD at the provided URL
        :param login_page_url: the URL of the login page,
            (e.g. https://localhost:8000/accounts/login/).
            Note that it's a security flaw to use HTTP for anything but local testing.
        :return: an authentication object that encapsulates the newly-created user session, or None
            if authentication failed (likely because of user error in entering credentials).
        :raises Exception: if an HTTP error occurs
        """

        # chop off the trailing '/', if any, so we can write easier-to-read URL snippets in our
        # code (starting w '%s/'). also makes our code trailing-slash agnostic.
        base_url = remove_trailing_slash(base_url)

        # issue a GET to get the CRSF token for use in auto-login

        login_page_url = "%s/accounts/login/" % base_url  # Django login page URL
        # login_page_url = '%s/rest/auth/login/' % base_url  # Django REST framework login page URL
        session = requests.session()
        response = session.get(login_page_url, timeout=timeout, verify=verify_ssl_cert)

        if response.status_code != requests.codes.ok:
            response.raise_for_status()

        # extract the CSRF token from the server response to include as a form header
        # with the login request (doesn't work without it, even though it's already present in the
        # session cookie). Note: NOT the same key as the header we send with requests

        csrf_token = response.cookies[DJANGO_CSRF_COOKIE_KEY]
        if not csrf_token:
            logger.error("No CSRF token received from EDD. Something's wrong.")
            raise Exception("Server response did not include the required CSRF token")

        # package up credentials and CSRF token to send with the login request
        login_dict = {"login": username, "password": password}
        csrf_request_headers = {"csrfmiddlewaretoken": csrf_token}
        login_dict.update(csrf_request_headers)

        # work around Django's additional CSRF protection for HTTPS, which doesn't apply outside of
        # the browser context
        headers = {}
        insert_spoofed_https_csrf_headers(headers, base_url)

        # issue a POST to log in
        response = session.post(
            login_page_url,
            data=login_dict,
            headers=headers,
            timeout=timeout,
            verify=verify_ssl_cert,
        )

        # return the session if it's successfully logged in, or print error messages/raise
        # exceptions as appropriate
        if response.status_code == requests.codes.ok:
            _DJANGO_LOGIN_FAILURE_CONTENT = "Login failed"
            _DJANGO_REST_API_FAILURE_CONTENT = "This field is required"

            # Note use of response.text here instead of response.content.  While EDD server-side
            # code is transitioning from Python 2 to 3, .text is important for converting response
            # to unicode so client can tolerate either bytestring or unicode content from server.
            if (
                _DJANGO_LOGIN_FAILURE_CONTENT in response.text
                or _DJANGO_REST_API_FAILURE_CONTENT in response.text
            ):
                logger.warning("Login failed. Please try again.")
                logger.info(response.headers)
                if debug:
                    show_response_html(response)
                return None
            else:
                logger.info("Successfully logged into EDD at %s" % base_url)
                return EddSessionAuth(session.cookies, csrf_token)
        else:
            if debug:
                show_response_html(response)
            response.raise_for_status()
