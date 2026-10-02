"""Offline protocol tests: no credentials, Azure requests, or resource writes."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import unittest
from contextlib import AsyncExitStack
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from retail import bootstrap as provisioning
from retail import foundry


def settings(**overrides):
    return NS(**{
        "environment": "azure",
        "foundry_project_endpoint": "https://example.services.ai.azure.com/api/projects/vitrine-retail",
        "agent_model_deployment": "retail-agent",
        "azure_client_id": "managed-identity-client-id",
        **overrides,
    })


def config():
    return {
        "id": "foundry-config",
        "pk": "store-demo",
        "agents": {
            "merchandising": {"name": "vitrine-merchandising", "version": "7"},
            "experiment": {"name": "vitrine-experiment", "version": "12"},
        },
    }


def snapshot(epoch="epoch-1"):
    return {
        "epochId": epoch,
        "evidence": [{"id": "ev-1", "description": "Receita simulada", "provenance": "simulated-pos"}],
        "availableActions": [
            {"id": "reposition-denim", "title": "Reposicionar denim", "applied": False},
            {"id": "replenish-essentials", "title": "Repor básicos", "applied": False},
        ],
        "metrics": {"revenueCents": 1234, "simulatedVisits": 7, "provenance": "simulated-pos"},
        "zones": [],
        "experiment": {"status": "not-started"},
        "interventions": [],
        "observations": [{
            "id": "obs-1", "status": "unavailable", "people": None,
            "provenance": "model-inference", "frameUri": "/api/vision/frames/camera.jpg",
        }],
    }


def advice():
    return {
        "summary": "Hipótese de melhoria na simulação, sem comprovação causal real.",
        "recommendations": [{
            "actionId": "reposition-denim",
            "title": "Reposicionar denim",
            "rationale": "A receita simulada justifica avaliar a hipótese.",
            "evidenceIds": ["ev-1"],
            "caveats": ["Efeito programado, não evidência de ganho real."],
            "priority": "medium",
            "requiresApproval": True,
        }],
        "limitations": ["Dados sintéticos; observação visual indisponível."],
    }


def call(call_id="call-1", name="read_demo_context", arguments="{}"):
    return NS(type="function_call", call_id=call_id, name=name, arguments=arguments)


def response(identifier="resp-1", calls=None, output=None, status="completed", **overrides):
    return NS(**{
        "id": identifier,
        "status": status,
        "output": calls or [],
        "output_text": json.dumps(advice() if output is None else output, ensure_ascii=False),
        "error": None,
        **overrides,
    })


def clients():
    openai = NS(
        conversations=NS(create=AsyncMock(return_value=NS(id="conv-1"))),
        responses=NS(create=AsyncMock(side_effect=[
            response(calls=[call()]), response("resp-2"),
        ])),
        close=AsyncMock(),
    )
    project = NS(
        get_openai_client=Mock(return_value=openai),
        agents=NS(create_version=AsyncMock(), get_version=AsyncMock()),
        close=AsyncMock(),
    )
    return project, openai


def sdk_modules(project):
    """Small SDK protocol doubles, scoped with patch.dict rather than installed."""
    credential = NS(close=AsyncMock())
    aio = ModuleType("azure.ai.projects.aio")
    aio.AIProjectClient = Mock(return_value=project)
    identity = ModuleType("azure.identity.aio")
    identity.DefaultAzureCredential = Mock(return_value=credential)
    models = ModuleType("azure.ai.projects.models")
    models.FunctionTool = lambda **kw: NS(type="function", **kw)
    models.PromptAgentDefinition = lambda **kw: NS(kind="prompt", **kw)
    models.PromptAgentDefinitionTextOptions = lambda **kw: NS(**kw)
    models.TextResponseFormatJsonSchema = lambda **kw: NS(**{"type": "json_schema", **kw})
    return {
        "azure.ai.projects.aio": aio,
        "azure.identity.aio": identity,
        "azure.ai.projects.models": models,
    }, credential


class FoundryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = NS(
            read_document=AsyncMock(return_value=config()), write_document=AsyncMock(),
            initialize=AsyncMock(), close=AsyncMock(),
        )
        self.project, self.openai = clients()
        self.modules, self.credential = sdk_modules(self.project)
        self.sdk_patch = patch.dict(sys.modules, self.modules)
        self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)
        self.service = foundry.FoundryService(settings(), self.repo)
        self.addAsyncCleanup(self.service.close)
        self.audit = AsyncMock()

    async def start(self):
        await self.service.initialize()
        self.assertTrue(self.service.available)

    async def execute(self, **overrides):
        params = dict(kind="merchandising", question="O que avaliar?", snapshot=snapshot(), audit=self.audit)
        params.update(overrides)
        return await self.service.execute(**params)

    def receipts(self):
        return [json.loads(value) for value in self.audit.call_args.args[0]["toolCalls"]]

    async def test_initialize_is_read_only_and_pins_saved_versions(self):
        await self.start()
        await self.service.initialize()
        self.repo.read_document.assert_awaited_once_with("foundry-config", "store-demo")
        self.repo.write_document.assert_not_awaited()
        self.project.agents.create_version.assert_not_awaited()
        self.project.agents.get_version.assert_not_awaited()
        self.openai.responses.create.assert_not_awaited()
        self.project.get_openai_client.assert_called_once_with(max_retries=0, timeout=60.0)
        self.modules["azure.ai.projects.aio"].AIProjectClient.assert_called_once_with(
            endpoint=settings().foundry_project_endpoint,
            credential=self.credential, retry_total=0, connection_timeout=10, read_timeout=60.0,
        )
        self.modules["azure.identity.aio"].DefaultAzureCredential.assert_called_once_with(
            managed_identity_client_id="managed-identity-client-id",
            exclude_interactive_browser_credential=True,
        )

    async def test_configuration_absent_local_or_invalid_never_constructs_clients(self):
        variants = [
            {"environment": "local"}, {"foundry_project_endpoint": ""},
            {"foundry_project_endpoint": "https://example.openai.azure.com"},
            {"foundry_project_endpoint": "http://example.services.ai.azure.com/api/projects/vitrine-retail"},
            {"foundry_project_endpoint": settings().foundry_project_endpoint + "?api-version=preview"},
            {"foundry_project_endpoint": "https://example.services.ai.azure.com/api/projects/other"},
            {"agent_model_deployment": ""},
        ]
        for overrides in variants:
            with self.subTest(overrides=overrides):
                service = foundry.FoundryService(settings(**overrides), self.repo)
                await service.initialize()
                self.assertFalse(service.available)
                with self.assertRaises(foundry.FoundryUnavailable):
                    await service.execute("merchandising", "Oi", snapshot(), self.audit)
        self.repo.read_document.assert_not_awaited()
        self.modules["azure.ai.projects.aio"].AIProjectClient.assert_not_called()

    async def test_missing_or_malformed_config_unavailable(self):
        bad_versions = [None, 1, True, "", "latest", "0", "-2"]
        invalid = [None, {}, {"agents": {}}]
        for version in bad_versions:
            doc = config()
            doc["agents"]["experiment"]["version"] = version
            invalid.append(doc)
        doc = config()
        doc["agents"]["merchandising"]["name"] = "different-agent"
        invalid.append(doc)
        for document in invalid:
            with self.subTest(document=document):
                self.repo.read_document.return_value = document
                await self.service.initialize()
                self.assertFalse(self.service.available)
        self.modules["azure.ai.projects.aio"].AIProjectClient.assert_not_called()

    async def test_managed_protocol_and_generated_ids(self):
        await self.start()
        result = await self.execute()
        first, second = [entry.kwargs for entry in self.openai.responses.create.await_args_list]
        self.assertEqual(first["extra_body"], {
            "agent_reference": {"type": "agent_reference", "name": "vitrine-merchandising", "version": "7"},
        })
        self.assertEqual(first["conversation"], "conv-1")
        self.assertEqual(second["conversation"], "conv-1")
        self.assertNotIn("model", first)
        self.assertNotIn("instructions", first)
        self.assertNotIn("tools", first)
        self.assertNotIn("previous_response_id", second)
        self.assertEqual(first["tool_choice"], {"type": "function", "name": "read_demo_context"})
        self.assertNotIn("text", first, "The schema belongs to the persisted agent, not the invocation")
        self.assertEqual([item["role"] for item in first["input"]], ["user"],
                         "Managed agents own their system/developer instructions")
        self.assertEqual(first["input"][0]["type"], "message")
        self.assertEqual(first["input"][0]["content"][0]["type"], "input_text")
        self.assertFalse(first["parallel_tool_calls"])
        self.assertEqual(second["input"][0]["type"], "function_call_output")
        self.assertEqual(second["input"][0]["call_id"], "call-1")
        self.assertEqual(result["agentVersion"], "7")
        self.assertEqual(result["responseId"], "resp-2")
        self.assertEqual(result["conversationId"], "conv-1")
        UUID(result["recommendations"][0]["id"])
        self.assertIs(result["recommendations"][0]["requiresApproval"], True)
        self.assertIn(foundry.PROVENANCE_LIMITATION, result["limitations"])
        self.assertEqual(self.receipts()[0]["status"], "executed")
        self.assertEqual(result["toolCalls"], self.audit.call_args.args[0]["toolCalls"])
        self.repo.write_document.assert_not_awaited()
        self.project.agents.create_version.assert_not_awaited()

    async def test_experiment_uses_distinct_name_and_actual_version(self):
        await self.start()
        result = await self.execute(kind="experiment")
        reference = self.openai.responses.create.call_args.kwargs["extra_body"]["agent_reference"]
        self.assertEqual(reference["name"], "vitrine-experiment")
        self.assertEqual(reference["version"], "12")
        self.assertEqual(result["agentVersion"], "12")

    async def test_context_frozen_before_await_and_location_free(self):
        await self.start()
        source = snapshot()
        source["secret"] = "NOT_AUTHORIZED"
        source["evidence"][0]["url"] = "https://sensitive.invalid"
        source["evidence"][0]["description"] += " https://sensitive.invalid/path"
        source["observations"][0]["rawSimulatorPeople"] = [1, 2, 3]
        original = copy.deepcopy(source)

        async def mutate_on_audit(metadata):
            source["metrics"]["revenueCents"] = 9999
            source["epochId"] = "changed"
            metadata["toolCalls"].append("corruption")

        await self.execute(snapshot=source, audit=mutate_on_audit)
        encoded = self.openai.responses.create.call_args.kwargs["input"][0]["output"]
        context = json.loads(encoded)
        self.assertEqual(context["epochId"], original["epochId"])
        self.assertEqual(context["metrics"], original["metrics"])
        self.assertIsNone(context["observations"][0]["people"])
        for disallowed in ("sensitive.invalid", "frameUri", "secret", "rawSimulatorPeople"):
            self.assertNotIn(disallowed, encoded)

    async def test_repeated_tool_reads_identical_and_four_response_bound(self):
        await self.start()
        self.openai.responses.create.side_effect = [
            response("r1", [call("c1")]), response("r2", [call("c2")]),
            response("r3", [call("c3")]), response("r4"),
        ]
        result = await self.execute()
        requests = [c.kwargs for c in self.openai.responses.create.await_args_list]
        outputs = [request["input"][0]["output"] for request in requests[1:]]
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(requests[-1]["tool_choice"], "none")
        self.assertEqual(len(result["toolCalls"]), 3)

    async def test_fourth_tool_call_rejected_without_fifth_response(self):
        await self.start()
        self.openai.responses.create.side_effect = [
            response(f"r{i}", [call(f"c{i}")]) for i in range(4)
        ]
        with self.assertRaisesRegex(foundry.FoundryError, "Limite"):
            await self.execute()
        self.assertEqual(self.openai.responses.create.await_count, 4)
        self.assertEqual(self.receipts()[-1]["status"], "rejected")

    async def test_batch_of_four_calls_cannot_bypass_budget(self):
        await self.start()
        self.openai.responses.create.side_effect = [response(calls=[call(f"c{i}") for i in range(4)])]
        with self.assertRaisesRegex(foundry.FoundryError, "Limite"):
            await self.execute()
        self.assertEqual(self.openai.responses.create.await_count, 1)
        self.assertEqual([x["status"] for x in self.receipts()], ["executed"] * 3 + ["rejected"])

    async def test_invalid_tools_and_arguments_audited_not_executed(self):
        await self.start()
        for item in [
            call(name="write_document"), call(arguments='{"url":"https://example.invalid"}'),
            call(arguments='{"sql":"SELECT *"}'), call(arguments='{"anything":1}'),
            call(arguments="null"), call(arguments="[]"), call(arguments="{bad"),
            call(arguments={}), call(call_id=""),
        ]:
            with self.subTest(item=item):
                self.audit.reset_mock()
                self.openai.responses.create.reset_mock()
                self.openai.responses.create.side_effect = [response(calls=[item])]
                with self.assertRaises(foundry.FoundryError):
                    await self.execute()
                self.assertEqual(self.openai.responses.create.await_count, 1)
                self.assertEqual(self.receipts()[0]["status"], "rejected")
                self.assertEqual(self.audit.call_args.args[0]["responseId"], "resp-1")

    async def test_reused_call_id_rejected(self):
        await self.start()
        self.openai.responses.create.side_effect = [
            response("r1", [call("same")]), response("r2", [call("same")]),
        ]
        with self.assertRaisesRegex(foundry.FoundryError, "repetido"):
            await self.execute()
        self.assertEqual(self.receipts()[-1]["status"], "rejected")

    async def test_unknown_evidence_action_and_applied_action_rejected(self):
        await self.start()
        for mode in ("evidence", "unknown_action", "applied", "duplicate"):
            payload = advice()
            context = snapshot()
            if mode == "evidence":
                payload["recommendations"][0]["evidenceIds"] = ["made-up"]
            elif mode == "unknown_action":
                payload["recommendations"][0]["actionId"] = "delete-stock"
            elif mode == "applied":
                context["availableActions"][0]["applied"] = True
            else:
                payload["recommendations"] *= 2
            with self.subTest(mode=mode):
                self.openai.responses.create.side_effect = [
                    response(calls=[call()]), response("final", output=payload),
                ]
                with self.assertRaises(foundry.FoundryError) as failure:
                    await self.execute(snapshot=context)
                self.assertEqual(failure.exception.metadata["responseId"], "final")

    async def test_strict_schema_no_extra_ids_false_approval_or_coercion(self):
        await self.start()
        mutations = [
            ("requiresApproval", False), ("requiresApproval", 1), ("requiresApproval", "true"),
            ("id", "model-made-id"), ("priority", "urgent"), ("title", 123),
            ("evidenceIds", []), ("caveats", []),
        ]
        for key, value in mutations:
            payload = advice()
            payload["recommendations"][0][key] = value
            with self.subTest(key=key, value=value):
                self.openai.responses.create.side_effect = [
                    response(calls=[call()]), response("final", output=payload),
                ]
                with self.assertRaisesRegex(foundry.FoundryError, "esquema estrito"):
                    await self.execute()

    async def test_invalid_json_refusal_and_incomplete_are_not_fallbacks(self):
        await self.start()
        invalid_json = response("invalid")
        invalid_json.output_text = "```json\n{}\n```"
        refusal = response("refusal", calls=[NS(type="message", content=[NS(type="refusal")])])
        for final in (invalid_json, refusal, response("incomplete", status="incomplete"),
                      response("failed", status="failed", error={"code": "server_error"})):
            with self.subTest(identifier=final.id):
                self.openai.responses.create.side_effect = [response(calls=[call()]), final]
                with self.assertRaises(foundry.FoundryError):
                    await self.execute()
                self.assertEqual(self.audit.call_args.args[0]["responseId"], final.id)

    async def test_no_context_read_no_success(self):
        await self.start()
        self.openai.responses.create.side_effect = [response()]
        with self.assertRaisesRegex(foundry.FoundryError, "não consultou"):
            await self.execute()

    async def test_response_ids_required_and_mismatches_audited(self):
        await self.start()
        for first in (
            response(identifier=None, calls=[call()]),
            response(calls=[call()], conversation={"id": "other-conversation"}),
            response(calls=[call()], agent_reference={"name": "vitrine-merchandising", "version": "99"}),
        ):
            with self.subTest(first=first):
                self.openai.responses.create.side_effect = [first]
                with self.assertRaises(foundry.FoundryError):
                    await self.execute()
        self.assertEqual(self.audit.call_args.args[0]["agentVersion"], "99")

    async def test_network_error_no_retry_keeps_conversation_and_previous_response(self):
        await self.start()
        self.openai.responses.create.side_effect = [response(calls=[call()]), OSError("offline")]
        with self.assertRaises(foundry.FoundryError) as failure:
            await self.execute()
        self.assertEqual(self.openai.responses.create.await_count, 2)
        self.assertEqual(failure.exception.metadata["responseId"], "resp-1")
        self.assertEqual(failure.exception.metadata["conversationId"], "conv-1")
        self.assertEqual(json.loads(failure.exception.metadata["toolCalls"][0])["status"], "executed")

    async def test_partial_sdk_error_metadata_and_tools_are_audited(self):
        await self.start()
        failure = RuntimeError("SDK error")
        failure.body = {"response": {
            "id": "partial", "conversation": {"id": "conv-1"},
            "output": [{"type": "function_call", "name": "read_demo_context", "call_id": "partial-call"}],
        }}
        self.openai.responses.create.side_effect = failure
        with self.assertRaises(foundry.FoundryError):
            await self.execute()
        self.assertEqual(self.audit.call_args.args[0]["responseId"], "partial")
        self.assertEqual(self.receipts()[0]["callId"], "partial-call")
        self.assertEqual(self.receipts()[0]["status"], "received")

    async def test_timeout_flushes_received_metadata_outside_deadline(self):
        await self.start()
        updates = []
        delayed_once = False

        async def slow_audit(update):
            nonlocal delayed_once
            if update["responseId"] and not delayed_once:
                delayed_once = True
                await asyncio.sleep(1)
            updates.append(update)

        with patch.object(foundry, "EXECUTION_TIMEOUT", 0.02):
            with self.assertRaisesRegex(foundry.FoundryError, "60 segundos") as failure:
                await self.execute(audit=slow_audit)
        self.assertEqual(updates[-1]["responseId"], "resp-1")
        self.assertEqual(json.loads(updates[-1]["toolCalls"][0])["status"], "received")
        self.assertEqual(failure.exception.metadata, updates[-1])
        self.assertEqual(self.openai.responses.create.await_count, 1)

    async def test_audit_failure_exposes_metadata_and_never_returns_advice(self):
        await self.start()

        async def broken_audit(update):
            if update["responseId"]:
                raise OSError("storage down")

        with self.assertRaises(foundry.FoundryError) as failure:
            await self.execute(audit=broken_audit)
        self.assertEqual(failure.exception.metadata["responseId"], "resp-1")
        self.assertEqual(len(failure.exception.metadata["toolCalls"]), 1)
        self.assertEqual(self.openai.responses.create.await_count, 1)

    async def test_invalid_snapshot_fails_before_any_remote_call(self):
        await self.start()
        for mode in ("epoch", "missing", "nan", "duplicate", "oversize"):
            context = snapshot()
            if mode == "epoch":
                context["epochId"] = ""
            elif mode == "missing":
                del context["evidence"]
            elif mode == "nan":
                context["metrics"]["value"] = float("nan")
            elif mode == "duplicate":
                context["evidence"] *= 2
            else:
                context["metrics"]["text"] = "x" * foundry.MAX_CONTEXT_BYTES
            with self.subTest(mode=mode):
                with self.assertRaises(foundry.FoundryError):
                    await self.execute(snapshot=context)
        self.openai.conversations.create.assert_not_awaited()

    async def test_observations_are_limited_without_turning_missing_counts_into_zero(self):
        context = snapshot()
        context["observations"] *= 40
        original = copy.deepcopy(context)
        serialized, _, _ = foundry._context(context)
        payload = json.loads(serialized)
        self.assertEqual(len(payload["observations"]), 16)
        self.assertTrue(payload["contextLimits"]["observationsTruncated"])
        self.assertTrue(all(item["people"] is None for item in payload["observations"]))
        self.assertEqual(context, original)

    async def test_empty_recommendations_and_full_limitations_remain_valid(self):
        payload = advice()
        payload["recommendations"] = []
        payload["limitations"] *= 16
        result = foundry._validate_advice(json.dumps(payload), {"ev-1"}, set())
        self.assertEqual(len(result["limitations"]), 16)
        foundry.Advice.model_validate(result)
        self.assertIn(foundry.PROVENANCE_LIMITATION, result["limitations"][-1])

    async def test_concurrent_requests_do_not_share_context_or_conversations(self):
        await self.start()
        self.openai.conversations.create.side_effect = [NS(id="conv-A"), NS(id="conv-B")]
        seen = {}
        barrier = asyncio.Event()

        async def respond(**kwargs):
            conversation = kwargs["conversation"]
            if kwargs["input"][0].get("type") != "function_call_output":
                seen[conversation] = None
                if len(seen) == 2:
                    barrier.set()
                await barrier.wait()
                return response(conversation + "-tool", [call(conversation + "-call")])
            seen[conversation] = json.loads(kwargs["input"][0]["output"])["epochId"]
            return response(conversation + "-final")

        self.openai.responses.create.side_effect = respond
        a, b = await asyncio.gather(
            self.execute(snapshot=snapshot("A"), audit=AsyncMock()),
            self.execute(snapshot=snapshot("B"), audit=AsyncMock()),
        )
        self.assertEqual(seen[a["conversationId"]], "A")
        self.assertEqual(seen[b["conversationId"]], "B")
        self.assertNotEqual(a["recommendations"][0]["id"], b["recommendations"][0]["id"])

    async def test_close_all_clients_idempotently(self):
        await self.start()
        await self.service.close()
        await self.service.close()
        self.assertFalse(self.service.available)
        self.openai.close.assert_awaited_once()
        self.project.close.assert_awaited_once()
        self.credential.close.assert_awaited_once()

    async def test_constructor_failure_closes_already_created_resources(self):
        self.project.get_openai_client.side_effect = RuntimeError("constructor failed")
        with self.assertRaises(RuntimeError):
            await self.service.initialize()
        self.project.close.assert_awaited_once()
        self.credential.close.assert_awaited_once()
        self.assertFalse(self.service.available)


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project, self.openai = clients()
        self.modules, self.credential = sdk_modules(self.project)
        sdk_patch = patch.dict(sys.modules, self.modules)
        sdk_patch.start()
        self.addCleanup(sdk_patch.stop)
        self.repo = NS(initialize=AsyncMock(), write_document=AsyncMock(), close=AsyncMock())
        self.created = {}

        async def create_version(*, agent_name, definition):
            version = "42" if agent_name == "vitrine-merchandising" else "105"
            value = NS(id=agent_name + ":" + version, name=agent_name, version=version, definition=definition)
            self.created[agent_name] = value
            return value

        async def get_version(*, agent_name, agent_version):
            return self.created[agent_name]

        self.project.agents.create_version.side_effect = create_version
        self.project.agents.get_version.side_effect = get_version

    async def test_bootstrap_definitions_versions_and_single_publish(self):
        document = await provisioning.bootstrap(settings(), self.repo)
        self.assertEqual(document["id"], "foundry-config")
        self.assertEqual(document["pk"], "store-demo")
        self.assertEqual(document["agents"]["merchandising"]["version"], "42")
        self.assertEqual(document["agents"]["experiment"]["version"], "105")
        self.repo.write_document.assert_awaited_once_with(document)
        definitions = [entry.kwargs["definition"] for entry in self.project.agents.create_version.await_args_list]
        self.assertEqual(len(definitions), 2)
        self.assertNotEqual(definitions[0].instructions, definitions[1].instructions)
        for definition in definitions:
            self.assertEqual(definition.kind, "prompt")
            self.assertEqual(definition.model, "retail-agent")
            self.assertIn("português", definition.instructions)
            self.assertIn("programados", definition.instructions)
            self.assertIn("aprovação", definition.instructions)
            self.assertEqual(len(definition.tools), 1)
            tool = definition.tools[0]
            self.assertEqual(tool.name, "read_demo_context")
            self.assertTrue(tool.strict)
            self.assertEqual(tool.parameters, {
                "type": "object", "properties": {}, "required": [], "additionalProperties": False,
            })
            self.assertTrue(definition.text.format.strict)
            self.assertFalse(definition.text.format.schema["additionalProperties"])
        self.project.agents.get_version.assert_any_await(agent_name="vitrine-merchandising", agent_version="42")
        self.project.agents.get_version.assert_any_await(agent_name="vitrine-experiment", agent_version="105")
        self.project.get_openai_client.assert_not_called()
        self.project.close.assert_awaited_once()
        self.credential.close.assert_awaited_once()

    async def test_second_creation_failure_never_publishes_partial_config(self):
        first_create = self.project.agents.create_version.side_effect

        async def failing_create(**kwargs):
            if kwargs["agent_name"] == "vitrine-experiment":
                raise RuntimeError("creation failed")
            return await first_create(**kwargs)

        self.project.agents.create_version.side_effect = failing_create
        with self.assertRaises(RuntimeError):
            await provisioning.bootstrap(settings(), self.repo)
        self.repo.write_document.assert_not_awaited()
        self.assertEqual(self.project.agents.create_version.await_count, 2)
        self.project.close.assert_awaited_once()
        self.credential.close.assert_awaited_once()

    async def test_unverified_version_or_definition_never_published(self):
        valid_get = self.project.agents.get_version.side_effect
        for mismatch in ("version", "model", "tools", "schema"):
            async def bad_get(**kwargs):
                value = copy.deepcopy(await valid_get(**kwargs))
                if mismatch == "version":
                    value.version = "999"
                elif mismatch == "model":
                    value.definition.model = "wrong"
                elif mismatch == "tools":
                    value.definition.tools[0].parameters["properties"]["url"] = {"type": "string"}
                else:
                    value.definition.text.format.strict = False
                return value

            with self.subTest(mismatch=mismatch):
                self.project.agents.get_version.side_effect = bad_get
                with self.assertRaises(foundry.FoundryError):
                    await provisioning.bootstrap(settings(), self.repo)
                self.repo.write_document.assert_not_awaited()

    async def test_invalid_settings_do_not_create_any_resources(self):
        with self.assertRaises(foundry.FoundryError):
            await provisioning.bootstrap(settings(environment="local"), self.repo)
        self.modules["azure.ai.projects.aio"].AIProjectClient.assert_not_called()
        self.repo.write_document.assert_not_awaited()

    async def test_module_entrypoint_initializes_and_closes_repository(self):
        config_module = ModuleType("retail.config")
        config_module.Settings = NS(from_env=Mock(return_value=settings()))
        storage_module = ModuleType("retail.storage")
        storage_module.create_repository = Mock(return_value=self.repo)
        with patch.dict(sys.modules, {"retail.config": config_module, "retail.storage": storage_module}):
            with patch("builtins.print") as output:
                await provisioning.main()
        self.repo.initialize.assert_awaited_once()
        self.repo.close.assert_awaited_once()
        self.repo.write_document.assert_awaited_once()
        self.assertEqual(output.call_count, 2)

    async def test_entrypoint_closes_repository_if_initialization_fails(self):
        config_module = ModuleType("retail.config")
        config_module.Settings = NS(from_env=Mock(return_value=settings()))
        storage_module = ModuleType("retail.storage")
        storage_module.create_repository = Mock(return_value=self.repo)
        self.repo.initialize.side_effect = RuntimeError("storage unavailable")
        with patch.dict(sys.modules, {"retail.config": config_module, "retail.storage": storage_module}):
            with self.assertRaises(RuntimeError):
                await provisioning.main()
        self.repo.close.assert_awaited_once()
        self.project.agents.create_version.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
