import inspect
import urllib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from mimetypes import guess_type

from loguru import logger
from dateutil import parser
from fastapi.responses import Response

# From https://stackoverflow.com/questions/1094841/get-a-human-readable-version-of-a-file-size
def humanize_bytes(num, suffix="B"):
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f} Yi{suffix}"


def remove_prefix(prefix, key):
    """ Remove prefix from the key, and then the leading slash.
    """
    if key and prefix:
        return key.removeprefix(prefix).removeprefix('/')
    return key


def dir_path(path):
    """ Ensure that the given path ends in a slash, 
        indicating that it points to a folder and not an object.
    """
    if path and not path.endswith('/'):
        return path + '/'
    return path


def add_elem(parent, key):
    """ Add a new child element to the given XML parent.
    """
    return ET.SubElement(parent, key)


def add_telem(parent, key, value):
    """ Add a text element as a child of the given XML parent.
    """
    if not value:
        return None
    elem = add_elem(parent, key)
    elem.text = str(value)
    return elem


def elem_to_str(elem):
    """ Render the given XML element to a string.
    """
    return ET.tostring(elem, encoding="utf-8", xml_declaration=True)


def parse_xml(xml):
    """ Parse the given XML string into an XML element.
    """
    return ET.fromstring(xml)


def url_encode(s):
    if not s: return None
    # AWS does something slightly strange here, maybe like this?
    return urllib.parse.quote(s).replace('%20','+')


def get_bucket_list_xml(buckets):
    
    root = ET.Element("ListAllMyBucketsResult")
    buckets_elem = add_elem(root, "Buckets")

    for bucket in buckets:
        bucket_elem = add_elem(buckets_elem, "Bucket")
        add_telem(bucket_elem, "Name", bucket)

    return elem_to_str(root)


def get_list_xml(contents, common_prefixes, **kwargs):
    """ Creates S3-style XML elements for the given object listing.
    """

    is_url_encode = False
    if 'EncodingType' in kwargs:
        is_url_encode = kwargs['EncodingType']=='url'

    root = ET.Element("ListBucketResult")

    keys = [
        'Name', 
        'Prefix',
        'Delimiter',
        'KeyCount',
        'MaxKeys',
        'EncodingType',
        'IsTruncated',
        'ContinuationToken',
        'NextContinuationToken',
        'StartAfter,'
    ]

    for key in keys:
        value = kwargs.get(key)
        if is_url_encode and key in ['Delimiter', 'Prefix', 'Key', 'StartAfter']:
            value = url_encode(value)
        add_telem(root, key, value)

    if common_prefixes:
        for cp in common_prefixes:
            value = cp
            if is_url_encode:
                value = url_encode(value)
            common_prefixes_elem = add_elem(root, "CommonPrefixes")
            add_telem(common_prefixes_elem, "Prefix", value)

    if contents:
        for obj in contents:
            key = obj["Key"]
            if is_url_encode:
                key = url_encode(key)
            contents_elem = add_elem(root, "Contents")
            add_telem(contents_elem, "Key", key)
            add_telem(contents_elem, "ETag", obj.get("ETag"))
            add_telem(contents_elem, "Size", obj.get("Size"))
            add_telem(contents_elem, "LastModified", obj.get("LastModified"))
            add_telem(contents_elem, "StorageClass", obj.get("StorageClass"))

    return elem_to_str(root)


def format_timestamp_s3(timestamp):
    """ Format the given timestamp to ISO date format compatible with AWS S3.
    """
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.isoformat()


def format_isoformat_as_local(isodate):
    """ Given a date formatted with ISO format, parse it and output it as a 
        local date string for human consumption.
    """
    # Parse it
    dt = parser.isoparse(isodate)
    # Convert it to the local timezone
    dt = dt.astimezone()
    # Format it for humans
    return dt.strftime("%Y-%m-%d at %I:%M %p")


def get_nosuchkey_response(key):
    return Response(content=inspect.cleandoc(f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <Error>
            <Code>NoSuchKey</Code>
            <Message>The specified key does not exist.</Message>
            <Key>{key}</Key>
        </Error>
        """), status_code=404, media_type="application/xml")


def get_nosuchbucket_response(bucket_name):
    return Response(content=inspect.cleandoc(f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <Error>
            <Code>NoSuchBucket</Code>
            <Message>The specified bucket does not exist</Message>
            <BucketName>{bucket_name}</BucketName>
        </Error>
        """), status_code=404, media_type="application/xml")


def get_accessdenied_response():
    return Response(content=inspect.cleandoc("""
        <?xml version="1.0" encoding="UTF-8"?>
        <Error>
            <Code>AccessDenied</Code>
            <Message>Access Denied</Message>
        </Error>
        """), status_code=403, media_type="application/xml")


def get_error_response(status_code, error_code, message, resource):
    return Response(content=inspect.cleandoc(f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <Error>
            <Code>{error_code}</Code>
            <Message>{message}</Message>
            <Resource>{resource}</Resource>
        </Error>
        """),
        status_code=status_code,
        media_type="application/xml")


def get_read_access_acl():
    """ Returns an S3 ACL that grants full read access
    """
    acl_xml = inspect.cleandoc("""
    <AccessControlPolicy>
        <Owner>
            <ID>1</ID>
            <DisplayName>unknown</DisplayName>
        </Owner>
        <AccessControlList>
            <Grant>
                <Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Group">
                    <URI>http://acs.amazonaws.com/groups/global/AllUsers</URI>
                </Grantee>
                <Permission>READ</Permission>
            </Grant>
        </AccessControlList>
    </AccessControlPolicy>
    """)
    return Response(content=acl_xml, media_type="application/xml")


def guess_content_type(filename):
    """ A wrapper for guess_type which deals with unknown MIME types
    """
    content_type, _ = guess_type(filename)
    if content_type:
        return content_type
    else:
        if filename.endswith('.yaml'):
            # Should be application/yaml but that doesn't display in current browsers
            # See https://httptoolkit.com/blog/yaml-media-type-rfc/
            return 'text/plain+yaml'
        else:
            return 'application/octet-stream'
