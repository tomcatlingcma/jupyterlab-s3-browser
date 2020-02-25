"""
Placeholder
"""
import base64
import json
import re
from collections import namedtuple

import boto3
import tornado.gen as gen
from notebook.base.handlers import APIHandler
from notebook.utils import url_path_join
from singleton_decorator import singleton
from traitlets import Unicode
from traitlets.config import SingletonConfigurable


class S3Config(SingletonConfigurable):
    """
    Allows configuration of access to an S3 api
    """

    endpoint_url = Unicode("", config=True, help="The url for the S3 api")
    client_id = Unicode("", config=True, help="The client ID for the S3 api")
    client_secret = Unicode("", config=True, help="The client secret for the S3 api")


@singleton
class S3Resource:  # pylint: disable=too-few-public-methods
    """
    Singleton wrapper around a boto3 resource
    """

    def __init__(self, config):
        config = S3Config().instance(config=config)
        if config.endpoint_url and config.client_id and config.client_secret:

            self.s3_resource = boto3.resource(
                "s3",
                aws_access_key_id=config.client_id,
                aws_secret_access_key=config.client_secret,
                endpoint_url=config.endpoint_url,
            )
        else:
            self.s3_resource = boto3.resource("s3")


def test_aws_s3_role_access():
    """
  Checks if we have access to AWS S3 through role-based access
  """
    test = boto3.resource("s3")
    all_buckets = test.buckets.all()
    result = [
        {"name": bucket.name + "/", "path": bucket.name + "/", "type": "directory"}
        for bucket in all_buckets
    ]
    assert result


def test_s3_credentials(endpoint_url, client_id, client_secret):
    """
    Checks if we're able to list buckets with these credentials.
    If not, it throws an exception.
    """
    test = boto3.resource(
        "s3",
        aws_access_key_id=client_id,
        aws_secret_access_key=client_secret,
        endpoint_url=endpoint_url,
    )
    all_buckets = test.buckets.all()
    result = [
        {"name": bucket.name + "/", "path": bucket.name + "/", "type": "directory"}
        for bucket in all_buckets
    ]
    assert result


class AuthHandler(APIHandler):  # pylint: disable=abstract-method
    """
    handle api requests to change auth info
    """

    @gen.coroutine
    def get(self, path=""):
        """
        Checks if the user is already authenticated
        against an s3 instance.
        """
        authenticated = False
        try:
            test_aws_s3_role_access()
            # if no exceptions, assume authenticated
            authenticated = True
        except Exception as err:
            print(err)

        if not authenticated:

            try:
                config = S3Config.instance()
                if config.endpoint_url and config.client_id and config.client_secret:
                    test_s3_credentials(
                        config.endpoint_url, config.client_id, config.client_secret
                    )

                    # If no exceptions were encountered during testS3Credentials,
                    # then assume we're authenticated
                    authenticated = True

            except Exception as err:
                # If an exception was encountered,
                # assume that we're not yet authenticated
                # or invalid credentials were provided
                print(err)

        self.finish(json.dumps({"authenticated": authenticated}))

    @gen.coroutine
    def post(self, path=""):
        """
        Sets s3 credentials.
        """

        try:
            req = json.loads(self.request.body)
            endpoint_url = req["endpoint_url"]
            client_id = req["client_id"]
            client_secret = req["client_secret"]

            test_s3_credentials(endpoint_url, client_id, client_secret)

            c = S3Config.instance()
            c.endpoint_url = endpoint_url
            c.client_id = client_id
            c.client_secret = client_secret
            S3Resource(self.config)

            self.finish(json.dumps({"success": True}))
        except Exception as err:
            self.finish(json.dumps({"success": False, "message": str(err)}))


class S3Handler(APIHandler):
    """
    Handles requests for getting S3 objects
    """

    s3 = None  # an S3Resource instance to be used for requests

    def parse_bucket_name_and_path(self, raw_path):
        _, bucket_name, path = raw_path.split('/',2)
        return (bucket_name, path)

    @gen.coroutine
    def get(self, raw_path="/"):
        """
        Takes a path and returns lists of files/objects
        and directories/prefixes based on the path.
        """

        try:
            if not self.s3:
                self.s3 = S3Resource(self.config).s3_resource

            if raw_path == "/":
                # requesting the root path, just return all buckets
                all_buckets = self.s3.buckets.all()
                result = [
                    {"name": bucket.name, "path": f"{bucket.name}/", "type": "directory"}
                    for bucket in all_buckets
                ]
            else:
                bucket_name, path = self.parse_bucket_name_and_path(raw_path)
                bucket = self.s3.Bucket(bucket_name)
                
                # resource is buggy with delimeters, use the client instead
                response = bucket.meta.client.list_objects_v2(Bucket = bucket_name, Prefix=path, Delimiter='/')
                
                result = []
                single_item = False
                
                if response.get('Contents'):
                    for obj in response['Contents']:
                        # deal with the case of a single object requested directly
                        if obj['Key'] == path:
                            result = {
                                "path": "{bucket_name}/{path}",
                                "type": "file",
                                "mimetype": obj.content_type,
                                "content": base64.encodebytes(
                                    s3.Object(bucket_name, obj['Key']).get()["Body"].read()
                                ).decode("ascii"),
                            }
                            single_item = True
                            break
                        else:
                            result.append(
                                {
                                    "name": obj['Key'].split('/')[-1],
                                    "path": f"{bucket_name}/{obj['Key']}",
                                    "type": "file",
                                    "mimetype": s3.Object(bucket_name, obj['Key']).content_type,
                                }
                            )
                            
                if response.get('CommonPrefixes') and not single_item:
                    for obj in response['CommonPrefixes']:
                        result.append(
                            {
                                "name": obj['Prefix'].split('/')[-2]+'/',
                                "path": f"{bucket_name}/{obj['Prefix']}",
                                "type": "directory",
                                "mimetype": "json",
                            } 
                        )
                
                if not result:
                    result = {
                        "error": 404,
                        "message": f"{raw_path} could not be found.",
                    }
        except Exception as e:
            print(e)
            result = {
                "error": 500, 
                "message": f"Path: {raw_path} Error: {str(e)}"
            }

        self.finish(json.dumps(result))


def _jupyter_server_extension_paths():
    return [{"module": "jupyterlab_s3_browser"}]


def load_jupyter_server_extension(nb_server_app):
    """
    Called when the extension is loaded.

    Args:
        nb_server_app (NotebookWebApplication):
        handle to the Notebook webserver instance.
    """
    web_app = nb_server_app.web_app
    base_url = web_app.settings["base_url"]
    endpoint = url_path_join(base_url, "s3")
    handlers = [
        (url_path_join(endpoint, "auth") + "(.*)", AuthHandler),
        (url_path_join(endpoint, "files") + "(.*)", S3Handler),
    ]
    web_app.add_handlers(".*$", handlers)
