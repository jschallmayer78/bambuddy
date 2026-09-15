"""The ingress prefix is attacker-controllable, so it is validated, not trusted.

``X-Ingress-Path`` reaches this code from a request header. On the Supervisor's
path it is a session prefix; from anywhere else on the network it is whatever
the sender felt like writing. These tests pin the difference.
"""

from types import SimpleNamespace

import pytest

from backend.app.core.ingress import (
    base_href,
    ingress_prefix,
    inject_base_href,
    is_ingress_request,
)


def request_with(header_value=None):
    headers = {"X-Ingress-Path": header_value} if header_value is not None else {}
    return SimpleNamespace(headers=headers)


class TestPrefixValidation:
    def test_home_assistants_own_shape_is_accepted(self):
        request = request_with("/api/hassio_ingress/ZmFrZS1zZXNzaW9u")
        assert ingress_prefix(request) == "/api/hassio_ingress/ZmFrZS1zZXNzaW9u"
        assert base_href(request) == "/api/hassio_ingress/ZmFrZS1zZXNzaW9u/"
        assert is_ingress_request(request) is True

    def test_a_trailing_slash_is_normalised_away(self):
        assert ingress_prefix(request_with("/api/hassio_ingress/abc/")) == "/api/hassio_ingress/abc"

    def test_no_header_means_direct_access(self):
        assert ingress_prefix(request_with()) == ""
        assert base_href(request_with()) == "/"
        assert is_ingress_request(request_with()) is False

    @pytest.mark.parametrize(
        "value",
        [
            "//evil.example",  # protocol-relative: would point <base> at another origin
            "https://evil.example/x",  # absolute URL
            "/api/../../etc",  # traversal
            "/api/hassio_ingress/a b",  # whitespace
            '/api/hassio_ingress/a"><script>',  # attribute break-out
            "/api/hassio_ingress/a\nb",  # header injection leftovers
            "api/hassio_ingress/abc",  # not absolute
            "/" + "a" * 300,  # absurd length
        ],
    )
    def test_anything_else_is_ignored_not_rejected(self, value):
        """Ignored, because the request is still a valid direct request."""
        request = request_with(value)
        assert ingress_prefix(request) == ""
        assert base_href(request) == "/"


class TestBaseTagInjection:
    def test_the_tag_goes_first_inside_head(self):
        html = b"<!doctype html><html><head><link rel=x href=y></head><body></body></html>"
        out = inject_base_href(html, "/api/hassio_ingress/abc/")
        assert b'<head><base href="/api/hassio_ingress/abc/">' in out
        # Before the first asset reference, or that reference resolves against
        # the wrong root.
        assert out.index(b"<base") < out.index(b"<link")

    def test_an_existing_tag_is_replaced_not_stacked(self):
        """The first base tag in a document wins — a stale one must not survive."""
        html = b'<html><head><base href="/"><title>x</title></head></html>'
        out = inject_base_href(html, "/api/hassio_ingress/abc/")
        assert out.count(b"<base") == 1
        assert b'<base href="/api/hassio_ingress/abc/">' in out

    def test_direct_access_gets_a_root_base(self):
        html = b'<html><head><base href="/"></head></html>'
        assert inject_base_href(html, "/") == html

    def test_the_href_is_escaped(self):
        html = b"<html><head></head></html>"
        out = inject_base_href(html, '/a"><script>alert(1)</script>')
        assert b"<script>alert(1)" not in out
        assert b"&quot;" in out

    def test_a_document_without_a_head_is_left_alone(self):
        html = b"<p>not a document</p>"
        assert inject_base_href(html, "/api/hassio_ingress/abc/") == html
