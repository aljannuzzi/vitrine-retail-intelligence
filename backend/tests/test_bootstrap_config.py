"""Offline configuration and entrypoint regressions for the bootstrap job."""

from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from retail import bootstrap as provisioning
from retail.app import create_app
from retail.config import Settings
from retail.foundry import FoundryError, configuration_problem


JOB_ENV = {
    "ENVIRONMENT": "azure",
    "AZURE_CLIENT_ID": "managed-identity-client-id",
    "COSMOS_ENDPOINT": "https://example.documents.azure.com:443/",
    "AZURE_STORAGE_ACCOUNT": "examplestorage",
    "FOUNDRY_PROJECT_ENDPOINT": (
        "https://example.services.ai.azure.com/api/projects/vitrine-retail"
    ),
    "AGENT_MODEL_DEPLOYMENT": "retail-agent",
}


class BootstrapConfigurationTests(unittest.TestCase):
    def test_job_environment_requires_no_http_secrets_or_origin(self):
        with patch.dict(os.environ, JOB_ENV, clear=True):
            settings = Settings.from_env(for_bootstrap=True)
        settings.validate(for_bootstrap=True)
        self.assertIsNone(configuration_problem(settings))
        self.assertEqual(settings.app_password, "")
        self.assertEqual(settings.session_secret, "")
        self.assertEqual(settings.public_origin, "")
        self.assertEqual(settings.azure_client_id, JOB_ENV["AZURE_CLIENT_ID"])
        self.assertFalse(settings.local_mode)
        with self.assertRaisesRegex(ValueError, "APP_PASSWORD"):
            settings.validate()

    def test_default_loader_and_api_reject_identical_job_environment(self):
        with patch.dict(os.environ, JOB_ENV, clear=True):
            with self.assertRaisesRegex(ValueError, "APP_PASSWORD"):
                Settings.from_env()
            with patch("retail.app.create_repository") as factory:
                with self.assertRaisesRegex(ValueError, "APP_PASSWORD"):
                    with TestClient(create_app(ticker=False)):
                        self.fail("API must not start with bootstrap-only configuration")
                factory.assert_not_called()

    def test_http_validation_remains_strict_for_each_http_field(self):
        with patch.dict(os.environ, JOB_ENV, clear=True):
            settings = Settings.from_env(for_bootstrap=True)
        valid_http = replace(
            settings, app_password="test-only-http-password",
            public_origin="https://testserver",
        )
        valid_http.validate()
        for field, value, error in (
            ("app_password", "", "APP_PASSWORD"),
            ("app_password", "short", "APP_PASSWORD"),
            ("session_secret", "short", "SESSION_SECRET"),
            ("public_origin", "", "PUBLIC_ORIGIN"),
            ("public_origin", "http://testserver", "PUBLIC_ORIGIN"),
            ("public_origin", "https://testserver/", "PUBLIC_ORIGIN"),
        ):
            with self.subTest(field=field, value=value):
                invalid_http = replace(valid_http, **{field: value})
                invalid_http.validate(for_bootstrap=True)
                with self.assertRaisesRegex(ValueError, error):
                    invalid_http.validate()

    def test_bootstrap_requires_identity_and_azure_configuration(self):
        for key in (
            "AZURE_CLIENT_ID", "COSMOS_ENDPOINT", "AZURE_STORAGE_ACCOUNT",
            "FOUNDRY_PROJECT_ENDPOINT",
        ):
            for value in (None, ""):
                with self.subTest(key=key, value=value):
                    environment = dict(JOB_ENV)
                    if value is None:
                        del environment[key]
                    else:
                        environment[key] = value
                    with patch.dict(os.environ, environment, clear=True):
                        with self.assertRaises(ValueError):
                            Settings.from_env(for_bootstrap=True)
                    with patch.dict(os.environ, JOB_ENV, clear=True):
                        settings = Settings.from_env(for_bootstrap=True)
                    with self.assertRaises(ValueError):
                        replace(settings, **{key.lower(): ""}).validate(for_bootstrap=True)

    def test_bootstrap_preserves_environment_and_cosmos_validation(self):
        for overrides in (
            {"ENVIRONMENT": "invalid"},
            {"LOCAL_MODE": "true"},
            {"ENVIRONMENT": "local"},
            {"COSMOS_ENDPOINT": "http://example.documents.azure.com"},
            {"AZURE_CLIENT_ID": "   "},
        ):
            with self.subTest(overrides=overrides):
                with patch.dict(os.environ, {**JOB_ENV, **overrides}, clear=True):
                    with self.assertRaises(ValueError):
                        Settings.from_env(for_bootstrap=True)


class BootstrapMainConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = SimpleNamespace(
            initialize=AsyncMock(), close=AsyncMock(), write_document=AsyncMock(),
        )
        self.factory = Mock(side_effect=self.create_repository)
        self.provision = AsyncMock(return_value={
            "agents": {
                "merchandising": {"name": "vitrine-merchandising", "version": "7"},
                "experiment": {"name": "vitrine-experiment", "version": "12"},
            },
        })
        for patcher in (
            patch.dict(os.environ, JOB_ENV, clear=True),
            patch("retail.storage.create_repository", self.factory),
            patch.object(provisioning, "bootstrap", self.provision),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.open_project = self.enterContext(patch.object(
            provisioning, "open_project",
            side_effect=AssertionError("No Azure clients may be opened"),
        ))

    def create_repository(self, settings, *, for_bootstrap=False):
        settings.validate(for_bootstrap=for_bootstrap)
        return self.repository

    async def test_main_uses_real_job_config_and_explicit_factory_flag_without_azure_writes(self):
        with patch("builtins.print") as output:
            await provisioning.main()
        settings = self.factory.call_args.args[0]
        self.factory.assert_called_once_with(settings, for_bootstrap=True)
        self.assertIsInstance(settings, Settings)
        self.assertEqual((settings.app_password, settings.session_secret, settings.public_origin),
                         ("", "", ""))
        self.provision.assert_awaited_once_with(settings, self.repository)
        self.repository.initialize.assert_awaited_once_with()
        self.repository.close.assert_awaited_once_with()
        self.repository.write_document.assert_not_called()
        self.open_project.assert_not_called()
        self.assertEqual(output.call_count, 2)
        output.assert_any_call("merchandising: vitrine-merchandising version=7")
        output.assert_any_call("experiment: vitrine-experiment version=12")

    async def test_main_rejects_missing_identity_or_endpoints_before_resources(self):
        for key in (
            "AZURE_CLIENT_ID", "COSMOS_ENDPOINT", "AZURE_STORAGE_ACCOUNT",
            "FOUNDRY_PROJECT_ENDPOINT",
        ):
            with self.subTest(key=key):
                environment = {name: value for name, value in JOB_ENV.items() if name != key}
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        await provisioning.main()
        self.factory.assert_not_called()
        self.provision.assert_not_called()
        self.open_project.assert_not_called()

    async def test_main_keeps_foundry_configuration_gate_before_resources(self):
        for overrides in (
            {"FOUNDRY_PROJECT_ENDPOINT": "https://example.openai.azure.com"},
            {"FOUNDRY_PROJECT_ENDPOINT": (
                "http://example.services.ai.azure.com/api/projects/vitrine-retail"
            )},
            {"AGENT_MODEL_DEPLOYMENT": ""},
            {"AGENT_MODEL_DEPLOYMENT": "other"},
            {"ENVIRONMENT": "local", "LOCAL_MODE": "true"},
        ):
            with self.subTest(overrides=overrides):
                with patch.dict(os.environ, {**JOB_ENV, **overrides}, clear=True):
                    with self.assertRaises(FoundryError):
                        await provisioning.main()
        self.factory.assert_not_called()
        self.provision.assert_not_called()
        self.open_project.assert_not_called()

    async def test_main_closes_repository_when_mocked_provisioning_fails(self):
        self.provision.side_effect = RuntimeError("mock provisioning failed")
        with self.assertRaisesRegex(RuntimeError, "mock provisioning failed"):
            await provisioning.main()
        self.repository.initialize.assert_awaited_once_with()
        self.repository.close.assert_awaited_once_with()
        self.repository.write_document.assert_not_called()
        self.open_project.assert_not_called()


if __name__ == "__main__":
    unittest.main()
