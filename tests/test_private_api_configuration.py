import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from cumulus_api import CumulusApi
from cumulus_api import main


FILE_ARN = 'arn:aws:lambda:us-west-2:123456789012:function:file-api'
ENV_ARN = 'arn:aws:lambda:us-east-1:123456789012:function:environment-api'


class TestPrivateApiConfiguration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.start_patch(patch.dict(os.environ, {}, clear=True))
        os.environ.update({
            'AWS_CONFIG_FILE': str(self.directory / 'aws-config'),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.directory / 'credentials'),
            'AWS_EC2_METADATA_DISABLED': 'true',
        })
        self.start_patch(patch.object(boto3, 'DEFAULT_SESSION', None))
        self.start_patch(patch('botocore.httpsession.URLLib3Session.send',
                               side_effect=AssertionError('Unexpected network request')))
        self.start_patch(patch('requests.sessions.Session.send',
                               side_effect=AssertionError('Unexpected network request')))
        self.client = boto3.Session(
            aws_access_key_id='testing', aws_secret_access_key='testing',
            region_name='us-east-1',
        ).client('lambda')
        self.addCleanup(self.client.close)
        self.real_setup = boto3.setup_default_session
        self.real_client = boto3.client
        self.setup = self.start_patch(patch.object(main.boto3, 'setup_default_session'))
        self.factory = self.start_patch(patch.object(main.boto3, 'client', return_value=self.client))
        self.start_patch(patch.object(main, 'CumulusToken', side_effect=AssertionError(
            'These configuration tests must not request Launchpad tokens')))
        self.start_patch(patch.object(main, 'Session', side_effect=AssertionError(
            'Private mode must not initialize HTTP transport')))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_config(self, **values):
        path = self.directory / 'config.cfg'
        path.write_text('[DEFAULT]\n' + ''.join(
            f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')
        return path

    def assert_version_call(self, api, expected_arn, client=None):
        client = self.client if client is None else client
        body = json.dumps({'body': json.dumps({'version': 'test-version'})}).encode()
        payload = {
            'httpMethod': 'GET', 'resource': '/{proxy+}', 'path': '/version',
            'headers': {'Content-Type': 'application/json', 'Cumulus-API-Version': '2'},
            'queryStringParameters': '', 'body': '',
        }
        with Stubber(client) as stubber:
            stubber.add_response('invoke', {
                'StatusCode': 200,
                'Payload': StreamingBody(io.BytesIO(body), len(body)),
            }, {'FunctionName': expected_arn, 'Payload': json.dumps(payload).encode()})
            self.assertEqual(api.get_version(), {'version': 'test-version'})
            stubber.assert_no_pending_responses()

    def assert_full_file_configuration(self):
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN,
                                 AWS_PROFILE='file-profile', AWS_REGION='us-west-2')
        original = path.read_bytes()
        api = CumulusApi(token='unused-private-mode-token', config_path=str(path))
        self.setup.assert_called_once_with(profile_name='file-profile', region_name='us-west-2')
        self.factory.assert_called_once_with('lambda')
        self.assert_version_call(api, FILE_ARN)
        self.assertEqual(path.read_bytes(), original)

    def test_file_private_settings_without_environment(self):
        self.assert_full_file_configuration()

    def test_file_private_settings_override_environment(self):
        os.environ.update(PRIVATE_API_LAMBDA_ARN=ENV_ARN,
                          AWS_PROFILE='environment-profile', AWS_REGION='us-east-1')
        self.assert_full_file_configuration()

    def test_file_profile_preserves_environment_region_fallback(self):
        os.environ.update(AWS_PROFILE='environment-profile', AWS_REGION='us-east-1')
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN, AWS_PROFILE='file-profile')
        api = CumulusApi(config_path=str(path))
        self.setup.assert_called_once_with(profile_name='file-profile', region_name='us-east-1')
        self.assert_version_call(api, FILE_ARN)

    def test_file_region_preserves_environment_profile_fallback(self):
        os.environ.update(AWS_PROFILE='environment-profile', AWS_REGION='us-east-1')
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN, AWS_REGION='us-west-2')
        api = CumulusApi(config_path=str(path))
        self.setup.assert_called_once_with(profile_name='environment-profile', region_name='us-west-2')
        self.assert_version_call(api, FILE_ARN)

    def test_file_arn_without_aws_overrides_uses_sdk_defaults(self):
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN)
        api = CumulusApi(config_path=str(path))
        self.setup.assert_called_once_with(profile_name=None, region_name=None)
        self.assert_version_call(api, FILE_ARN)

    def test_file_without_aws_overrides_keeps_environment_fallbacks(self):
        os.environ.update(AWS_PROFILE='environment-profile', AWS_REGION='us-east-1')
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN)
        api = CumulusApi(config_path=str(path))
        self.setup.assert_called_once_with(profile_name='environment-profile', region_name='us-east-1')
        self.assert_version_call(api, FILE_ARN)

    def test_environment_private_configuration(self):
        os.environ.update(PRIVATE_API_LAMBDA_ARN=ENV_ARN,
                          AWS_PROFILE='environment-profile', AWS_REGION='us-east-1')
        api = CumulusApi()
        self.setup.assert_called_once_with(profile_name='environment-profile', region_name='us-east-1')
        self.assert_version_call(api, ENV_ARN)

    def test_environment_private_configuration_without_aws_settings(self):
        os.environ['PRIVATE_API_LAMBDA_ARN'] = ENV_ARN
        api = CumulusApi()
        self.setup.assert_called_once_with(profile_name=None, region_name=None)
        self.assert_version_call(api, ENV_ARN)

    def assert_public_configuration(self, **kwargs):
        url = 'https://example.invalid/api'
        response = Mock(url=url + '/v1/version')
        response.json.return_value = {'version': 'public-version'}
        session = Mock()
        session.get.return_value = response
        with patch.object(main, 'Session', return_value=session):
            api = CumulusApi(token='public-token', **kwargs)
            self.assertEqual(api.get_version(), {'version': 'public-version'})
        session.get.assert_called_once_with(
            url=url + '/v1/version', json=None,
            headers={'Authorization': 'Bearer public-token', 'Cumulus-API-Version': '2'},
            auth=None,
        )
        self.setup.assert_not_called()
        self.factory.assert_not_called()

    def test_public_file_configuration_still_selects_http(self):
        os.environ['PRIVATE_API_LAMBDA_ARN'] = ENV_ARN
        path = self.write_config(INVOKE_BASE_URL='https://example.invalid/api/')
        self.assert_public_configuration(config_path=str(path))

    def test_public_environment_configuration_still_selects_http(self):
        os.environ['INVOKE_BASE_URL'] = 'https://example.invalid/api/'
        self.assert_public_configuration()

    def test_lambda_errors_remain_visible(self):
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN)
        api = CumulusApi(config_path=str(path))
        with Stubber(self.client) as stubber:
            stubber.add_client_error('invoke', service_error_code='ResourceNotFoundException')
            with self.assertRaises(ClientError) as error:
                api.get_version()
            self.assertEqual(error.exception.response['Error']['Code'], 'ResourceNotFoundException')
            stubber.assert_no_pending_responses()

    def test_file_settings_create_a_real_sdk_session(self):
        (self.directory / 'credentials').write_text(
            '[file-profile]\naws_access_key_id=testing\naws_secret_access_key=testing\n',
            encoding='utf-8')
        (self.directory / 'aws-config').write_text(
            '[profile file-profile]\nregion=us-west-1\n', encoding='utf-8')
        os.environ.update(AWS_PROFILE='missing-environment-profile',
                          AWS_REGION='us-east-1', PRIVATE_API_LAMBDA_ARN=ENV_ARN)
        path = self.write_config(PRIVATE_API_LAMBDA_ARN=FILE_ARN,
                                 AWS_PROFILE='file-profile', AWS_REGION='us-west-2')
        with patch.object(main.boto3, 'setup_default_session', self.real_setup), \
                patch.object(main.boto3, 'client', self.real_client):
            api = CumulusApi(config_path=str(path))
        self.addCleanup(api.lambda_client.close)
        self.assertEqual(boto3.DEFAULT_SESSION.profile_name, 'file-profile')
        self.assertEqual(api.lambda_client.meta.region_name, 'us-west-2')
        self.assert_version_call(api, FILE_ARN, api.lambda_client)


if __name__ == '__main__':
    unittest.main()
