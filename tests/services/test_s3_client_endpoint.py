"""The content bucket goes where S3_ENDPOINT_URL says.

The endpoint was a documented setting the code never passed to boto3, so a
bucket moved to Cloudflare R2 was still addressed to AWS — which answers an R2
key with "InvalidAccessKeyId" and reads like a credentials problem.
"""
import pytest

from app.services import s3_service

R2_ENDPOINT = "https://abc123.r2.cloudflarestorage.com"


@pytest.fixture
def store(monkeypatch):
    """Point the settings the client is built from at a given store."""
    def configure(endpoint_url="", region="eu-north-1"):
        monkeypatch.setattr(s3_service.settings, "s3_endpoint_url", endpoint_url)
        monkeypatch.setattr(s3_service.settings, "aws_region", region)
        return s3_service.build_content_client()

    return configure


def test_an_endpoint_is_where_requests_are_sent(store):
    client = store(endpoint_url=R2_ENDPOINT)
    assert client.meta.endpoint_url == R2_ENDPOINT


def test_no_endpoint_still_means_aws(store):
    client = store(endpoint_url="", region="eu-north-1")
    assert client.meta.endpoint_url == "https://s3.eu-north-1.amazonaws.com"
    assert client.meta.region_name == "eu-north-1"


def test_r2_is_signed_against_the_region_it_accepts(store):
    """R2 validates the SigV4 credential scope against "auto" and rejects a
    real AWS region, whatever AWS_REGION is left set to."""
    client = store(endpoint_url=R2_ENDPOINT, region="eu-north-1")
    assert client.meta.region_name == "auto"


def test_another_s3_compatible_store_keeps_the_configured_region(store):
    client = store(endpoint_url="http://localhost:9000", region="eu-north-1")
    assert client.meta.endpoint_url == "http://localhost:9000"
    assert client.meta.region_name == "eu-north-1"


def test_a_bare_host_is_read_as_https(store):
    """Cloudflare displays the R2 endpoint without a scheme, and boto3 rejects
    it as an invalid endpoint — while building the client, so a value pasted as
    shown stopped the API from starting at all."""
    client = store(endpoint_url="abc123.r2.cloudflarestorage.com")
    assert client.meta.endpoint_url == R2_ENDPOINT
    assert client.meta.region_name == "auto"


def test_a_trailing_slash_is_not_part_of_the_host(store):
    client = store(endpoint_url=R2_ENDPOINT + "/")
    assert client.meta.endpoint_url == R2_ENDPOINT


def test_an_http_endpoint_is_left_alone(store):
    """A local MinIO is plain http; the scheme is only supplied when missing."""
    client = store(endpoint_url="http://localhost:9000")
    assert client.meta.endpoint_url == "http://localhost:9000"


def test_surrounding_whitespace_does_not_become_an_endpoint(store):
    """An env var edited by hand often keeps a trailing space; treating that as
    a value sends every request to a host that does not resolve."""
    client = store(endpoint_url="   ", region="eu-north-1")
    assert client.meta.endpoint_url == "https://s3.eu-north-1.amazonaws.com"


def test_the_worker_builds_its_client_the_same_way(store, monkeypatch):
    """The API and Celery must address one store: a worker left on the old one
    leaves every upload stuck in "processing"."""
    from app.tasks import upload_tasks

    monkeypatch.delattr(upload_tasks._s3, "_client", raising=False)
    monkeypatch.setattr(s3_service.settings, "s3_endpoint_url", R2_ENDPOINT)
    monkeypatch.setattr(s3_service.settings, "aws_region", "eu-north-1")

    client = upload_tasks._s3()
    try:
        assert client.meta.endpoint_url == R2_ENDPOINT
        assert client.meta.region_name == "auto"
    finally:
        # Built lazily and cached on the function; drop it so the next test in
        # this process does not inherit the R2 client.
        if hasattr(upload_tasks._s3, "_client"):
            del upload_tasks._s3._client


def test_importing_the_module_does_not_build_a_client():
    """boto3 validates the endpoint as it constructs the client. Doing that at
    import turned a mistyped setting into a service that could not start — no
    health check, no login, nothing but a crash loop to read the error from."""
    import importlib

    module = importlib.reload(s3_service)
    try:
        assert module._client is None
    finally:
        importlib.reload(s3_service)


def test_an_unusable_endpoint_fails_the_call_not_the_import(monkeypatch):
    import importlib

    module = importlib.reload(s3_service)
    try:
        monkeypatch.setattr(module.settings, "s3_endpoint_url", "https://")
        with pytest.raises(ValueError):
            module._get_client()
    finally:
        importlib.reload(s3_service)
