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
