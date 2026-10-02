import json
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential


def main():
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    if ".services.ai.azure.com/api/projects/vitrine-retail" not in endpoint:
        raise ValueError("Unexpected project endpoint")
    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    agent_name = "vitrine-platform-probe"
    version = None
    try:
        version = project.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(
                model="retail-agent",
                instructions="Call read_probe once, then reply exactly VITRINE_FOUNDRY_OK. This is a synthetic platform integration check.",
                tools=[FunctionTool(
                    name="read_probe", description="Read fixed synthetic platform context.",
                    parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                    strict=True,
                )],
            ),
        )
        with project.get_openai_client(timeout=90.0, max_retries=1) as client:
            reference = {"agent_reference": {"type": "agent_reference", "name": agent_name}}
            conversation = client.conversations.create()
            inputs = "Perform the synthetic integration check."
            used_tool = False
            for _ in range(3):
                response = client.responses.create(
                    conversation=conversation.id, input=inputs, extra_body=reference, max_output_tokens=300
                )
                if response.status != "completed":
                    raise RuntimeError(f"Foundry response state: {response.status}")
                calls = [item for item in response.output if item.type == "function_call"]
                if not calls:
                    if not used_tool or "VITRINE_FOUNDRY_OK" not in response.output_text:
                        raise RuntimeError("Managed agent did not complete its tool protocol")
                    print(json.dumps({
                        "event": "managed_foundry_agent_verified",
                        "agentName": agent_name,
                        "version": version.version,
                        "responseId": response.id,
                        "conversationId": conversation.id,
                        "toolCalled": used_tool,
                    }), flush=True)
                    return
                inputs = []
                for call in calls:
                    if call.name != "read_probe" or json.loads(call.arguments) != {} or used_tool:
                        raise RuntimeError("Unexpected probe tool request")
                    used_tool = True
                    inputs.append({"type": "function_call_output", "call_id": call.call_id, "output": '{"synthetic":true,"available":true}'})
            raise RuntimeError("Probe exceeded its response budget")
    finally:
        if version is not None:
            project.agents.delete_version(agent_name=agent_name, agent_version=version.version)
        project.close()
        credential.close()


if __name__ == "__main__":
    main()
