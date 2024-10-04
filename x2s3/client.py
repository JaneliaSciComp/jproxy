class ProxyClient:
    """ Interface for a client that implements an S3-like interface 
        to key-value access against some backend service. 

        Note that this interface does not try to encode the entire S3 API.
        We only care about the bare-bones functionality that is required 
        for viewers like Neuroglancer, N5 Viewer, Vizarr, etc.
    """

    async def head_object(self, key: str):
        """
        Basic interface for AWS S3's HeadObject API.
        https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html
        """

    async def get_object(self, key: str, range_header: str = None):
        """
        Basic interface for AWS S3's GetObject API.
        https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html
        """

    async def list_objects_v2(self,
                            continuation_token: str,
                            delimiter: str,
                            encoding_type: str,
                            fetch_owner: str,
                            max_keys: str,
                            prefix: str,
                            start_after: str):
        """
        Basic interface for AWS S3's ListObjectsV2 API.
        https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html
        """
    