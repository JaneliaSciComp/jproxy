import urllib.parse

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from xml.etree.ElementTree import Element
from x2s3.app import create_app
from x2s3.settings import Target, Settings
from x2s3.utils import parse_xml

obj_path = '/janelia-data-examples/jrc_mus_lung_covid.n5/render/v1_acquire_align___20210609_224836/s0/0/0/0'

@pytest.fixture
def get_settings():
    settings = Settings()
    settings.base_url = HttpUrl('http://testserver')
    settings.virtual_buckets = True
    settings.targets = [
        Target(
            name='janelia-data-examples',
            options={'bucket':'janelia-data-examples'}
        ),
        Target(
            name='with-prefix',
            options={
                'bucket':'janelia-data-examples',
                'prefix':'jrc_mus_lung_covid.n5/'
            }
        ),
        Target(
            name='hidden-with-endpoint',
            browseable=False,
            options={
                'bucket':'janelia-data-examples',
                'endpoint':'https://s3.amazonaws.com',
            }
        )
    ]
    return settings


@pytest.fixture
def app(get_settings):
    return create_app(get_settings)


def test_acl(app):
    with TestClient(app) as client:
        response = client.get(f"/janelia-data-examples?acl")
        assert response.status_code == 200
        assert response.headers['content-type'] == "application/xml"
        assert response.text.count("<Grant>") == 1


def test_get_html_root(app):
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers['content-type'].startswith("text/html")
        for target in app.settings.targets:
            if target.browseable:
                assert target.name in response.text
            else:
                assert target.name not in response.text


def test_get_html_listing(app):
    with TestClient(app) as client:
        response = client.get("/janelia-data-examples/jrc_mus_lung_covid.n5/")
        assert response.status_code == 200
        assert response.headers['content-type'].startswith("text/html")
        assert '<html>' in response.text
        assert '<head>' in response.text
        assert 'attributes.json' in response.text


def test_list_objects(app):
    with TestClient(app) as client:
        bucket_name = 'janelia-data-examples'
        max_keys = 7
        response = client.get(f"/{bucket_name}?list-type=2&max-keys={max_keys}")
        assert response.status_code == 200
        root = parse_xml(response.text)
        assert root.tag == "ListBucketResult"
        assert root.find('Name').text == bucket_name

        # CommonPrefixes are only returned when there is a delimiter
        assert len(root.findall('CommonPrefixes')) == 0

        contents = root.findall('Contents')
        assert len(contents) <= max_keys
    
        if root.find('NextContinuationToken') is not None:
            assert root.find('IsTruncated').text == "true"

        assert isinstance(contents[0].find('Key'), Element)
        assert isinstance(contents[0].find('Size'), Element)
        assert isinstance(contents[0].find('LastModified'), Element)


def test_list_objects_delimiter(app):
    with TestClient(app) as client:
        bucket_name = 'janelia-data-examples'
        max_keys = 9
        response = client.get(f"/{bucket_name}?list-type=2&delimiter=/&max-keys={max_keys}")
        assert response.status_code == 200
        root = parse_xml(response.text)
        assert root.tag == "ListBucketResult"
        assert root.find('Name').text == bucket_name
        assert root.find('Delimiter').text == '/'
        assert len(root.findall('CommonPrefixes')) == 1
        assert root.find('IsTruncated').text == "false"


def test_list_objects_continuation(app):
    with TestClient(app) as client:
        bucket_name = 'janelia-data-examples'
        max_keys = 4
        uri = f"/{bucket_name}?list-type=2&max-keys={max_keys}&prefix=jrc_mus_lung_covid.n5/render/v1_acquire_align___20210609_224836/s0/0/0"
        
        token_param = ''
        total = 0
        c = 0
        while True:
            url = f"{uri}{token_param}"
            print(f"Fetching {url}")
            response = client.get(url)
            assert response.status_code == 200
            root = parse_xml(response.text)

            assert root.tag == "ListBucketResult"
            assert root.find('Name').text == bucket_name
            assert root.find('MaxKeys').text == str(max_keys)
            assert len(root.findall('CommonPrefixes')) == 0

            contents = root.findall('Contents')
            print(f"Got {len(contents)} results")
            total += len(contents)
            assert len(contents) <= max_keys

            next_token_elem = root.find('NextContinuationToken')
            if next_token_elem is not None:
                assert root.find('IsTruncated').text == "true"
            else:
                break

            safe_token = urllib.parse.quote_plus(next_token_elem.text)
            token_param = f"&continuation-token={safe_token}"
            
            c += 1
            if c>10:
                assert False

        assert total == 6


def test_head_object(app):
    with TestClient(app) as client:
        response = client.head("/janelia-data-examples/jrc_mus_lung_covid.n5/attributes.json")
        assert response.status_code == 200
        response = client.head("/janelia-data-examples/jrc_mus_lung_covid.n5/")
        assert response.status_code == 404


def test_prefixed_head_object(app):
    with TestClient(app) as client:
        response = client.head("/with-prefix/attributes.json")
        assert response.status_code == 200
        response = client.head("/with-prefix/render/attributes.json")
        assert response.status_code == 200
        response = client.head("/with-prefix/render/")
        assert response.status_code == 404


def test_get_object(app):
    with TestClient(app) as client:
        response = client.get("/janelia-data-examples/jrc_mus_lung_covid.n5/attributes.json")
        assert response.status_code == 200
        json_obj = response.json()
        assert 'n5' in json_obj


def test_prefixed_get_object(app):
    with TestClient(app) as client:
        response = client.get("/with-prefix/attributes.json")
        assert response.status_code == 200
        json_obj = response.json()
        assert 'n5' in json_obj


def test_virtual_host_get_object(app):
    with TestClient(app) as client:
        response = client.get("/jrc_mus_lung_covid.n5/attributes.json",
            headers={'Host':'janelia-data-examples.testserver'})
        assert response.status_code == 200
        json_obj = response.json()
        assert 'n5' in json_obj


def test_get_object_hidden(app):
    with TestClient(app) as client:
        response = client.get("/hidden-with-endpoint/jrc_mus_lung_covid.n5/attributes.json")
        assert response.status_code == 200
        json_obj = response.json()
        assert 'n5' in json_obj


def test_get_object_range_first(app):
    with TestClient(app) as client:
        # Test a valid range request (first 100 bytes)
        response = client.get(
            obj_path,
            headers={"Range": "bytes=0-99"}
        )
        assert response.status_code == 206  # Partial Content
        assert 'Content-Range' in response.headers
        assert response.headers['Content-Range'] == 'bytes 0-99/987143'
        assert len(response.content) == 100

def test_get_object_range_mid(app):
    with TestClient(app) as client:
        # Test a valid range request (bytes 100-199)
        response = client.get(
            obj_path,
            headers={"Range": "bytes=100-199"}
        )
        assert response.status_code == 206  # Partial Content
        assert 'Content-Range' in response.headers
        assert response.headers['Content-Range'] == 'bytes 100-199/987143'
        assert len(response.content) == 100

def test_get_object_range_last(app):
    with TestClient(app) as client:
        # Test a valid range request (last 100 bytes)
        response = client.get(
            obj_path,
            headers={"Range": "bytes=-100"}
        )
        assert response.status_code == 206  # Partial Content
        assert 'Content-Range' in response.headers
        assert len(response.content) == 100

def test_get_object_range_out_of_bounds(app):
    with TestClient(app) as client:
        # Test invalid range request (out of bounds)
        response = client.get(
            obj_path,
            headers={"Range": "bytes=1000000-2000000"}
        )
        assert response.status_code == 416  # Range Not Satisfiable
        root = parse_xml(response.text)
        assert root.find('Code').text == 'InvalidRange'


def test_prefixed_list_objects(app):
    with TestClient(app) as client:
        bucket_name = 'with-prefix'
        response = client.get(f"/{bucket_name}?list-type=2&delimiter=/")
        assert response.status_code == 200
        root = parse_xml(response.text)
        assert root.tag == "ListBucketResult"
        assert root.find('Name').text == bucket_name
        assert root.find('Delimiter').text == '/'
        assert len(root.findall('CommonPrefixes')) == 2
        assert root.find('IsTruncated').text == "false"


def test_get_object_missing(app):
    with TestClient(app) as client:
        response = client.get("/janelia-data-examples/missing")
        assert response.status_code == 404
        assert response.headers['content-type'] == "application/xml"


def test_bucket_missing(app):
    with TestClient(app) as client:
        response = client.get("/missing/attributes.json")
        assert response.status_code == 404
        assert response.headers['content-type'] == "application/xml"
        root = parse_xml(response.text)
        assert root.find('Code').text == 'NoSuchBucket'
        

def test_list_objects_error(app):
    with TestClient(app) as client:
        response = client.get(f"/janelia-data-examples?list-type=2&max-keys=aaa")
        assert response.status_code == 400
        assert response.headers['content-type'] == "application/json"


def test_get_object_precedence(app):
    with TestClient(app) as client:
        response = client.get(f"/janelia-data-examples/jrc_mus_lung_covid.n5/attributes.json?list-type=2")
        assert response.status_code == 200
        json_obj = response.json()
        assert 'n5' in json_obj
