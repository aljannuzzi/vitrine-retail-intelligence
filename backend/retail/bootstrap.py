"""Explicit provisioning entry point: python -m retail.bootstrap."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from .foundry import (
    AGENT_NAMES,
    COMMON_INSTRUCTIONS,
    CONFIG_ID,
    CONFIG_PK,
    EXECUTION_TIMEOUT,
    ROLE_INSTRUCTIONS,
    TOOL_NAME,
    FoundryError,
    _get,
    _version,
    advice_format,
    configuration_problem,
    open_project,
)


def agent_definition(kind: str, deployment: str) -> Any:
    from azure.ai.projects.models import (
        FunctionTool,
        PromptAgentDefinition,
        PromptAgentDefinitionTextOptions,
        TextResponseFormatJsonSchema,
    )

    return PromptAgentDefinition(
        model=deployment,
        instructions=COMMON_INSTRUCTIONS + "\n" + ROLE_INSTRUCTIONS[kind],
        tools=[
            FunctionTool(
                name=TOOL_NAME,
                description=(
                    "Lê apenas agregados e observações limitadas do retrato "
                    "autorizado desta execução. Sem parâmetros, URLs, SQL ou escrita."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                strict=True,
            )
        ],
        text=PromptAgentDefinitionTextOptions(
            format=TextResponseFormatJsonSchema(**advice_format())
        ),
    )


async def bootstrap(settings: Any, repository: Any) -> dict:
    """Create and verify both real versions before atomically publishing config."""
    problem = configuration_problem(settings)
    if problem:
        raise FoundryError(problem)
    async with AsyncExitStack() as resources:
        project = await open_project(settings, resources)
        agents = {}
        for kind, name in AGENT_NAMES.items():
            definition = agent_definition(kind, settings.agent_model_deployment)
            async with asyncio.timeout(EXECUTION_TIMEOUT):
                created = await project.agents.create_version(agent_name=name, definition=definition)
                version = _version(_get(created, "version"))
                if _get(created, "name") != name or not version:
                    raise FoundryError("Bootstrap não recebeu nome e versão reais do agente.")
                verified = await project.agents.get_version(agent_name=name, agent_version=version)
                if (
                    _get(verified, "name") != name
                    or _get(verified, "version") != version
                    or not _get(verified, "id")
                ):
                    raise FoundryError("Não foi possível verificar a versão criada do agente.")
                actual_definition = _get(verified, "definition")
                if (
                    _get(actual_definition, "kind") != "prompt"
                    or _get(actual_definition, "model") != settings.agent_model_deployment
                    or _get(actual_definition, "instructions") != _get(definition, "instructions")
                ):
                    raise FoundryError("A definição persistida do agente não corresponde ao bootstrap.")
                tools = _get(actual_definition, "tools", []) or []
                if (
                    len(tools) != 1
                    or _get(tools[0], "type") != "function"
                    or _get(tools[0], "name") != TOOL_NAME
                    or _get(tools[0], "strict") is not True
                    or _get(tools[0], "parameters") != _get(_get(definition, "tools")[0], "parameters")
                ):
                    raise FoundryError("A ferramenta persistida não corresponde ao contrato somente leitura.")
                actual_format = _get(_get(actual_definition, "text"), "format")
                expected_format = advice_format()
                if any(_get(actual_format, key) != value for key, value in expected_format.items()):
                    raise FoundryError("O esquema estruturado persistido não corresponde ao contrato.")
                agents[kind] = {"name": name, "version": version}
        document = {"id": CONFIG_ID, "pk": CONFIG_PK, "agents": agents}
        await repository.write_document(document)
        return document


async def main() -> None:
    from .config import Settings
    from .storage import create_repository

    settings = Settings.from_env(for_bootstrap=True)
    problem = configuration_problem(settings)
    if problem:
        raise FoundryError(problem)
    repository = create_repository(settings, for_bootstrap=True)
    try:
        await repository.initialize()
        document = await bootstrap(settings, repository)
        for kind, agent in document["agents"].items():
            print(f"{kind}: {agent['name']} version={agent['version']}")
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
