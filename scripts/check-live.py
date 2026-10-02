import argparse
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--exercise-agent", action="store_true")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    host = urlparse(base)
    if host.scheme != "https" or not host.hostname or not host.hostname.startswith("vitrine-api.") or not host.hostname.endswith(".azurecontainerapps.io"):
        raise ValueError("Only the independent VITRINE application is allowed")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Origin": base, "Content-Type": "application/json"},
        )
        with opener.open(req, timeout=180) as response:
            return json.load(response)

    assert request("/health")["status"] in ("ok", "ready")
    try:
        request("/api/snapshot")
        raise AssertionError("Anonymous snapshot access was allowed")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    password = os.environ.pop("VITRINE_PASSWORD")
    assert request("/api/session", "POST", {"password": password})["authenticated"]
    del password
    first = request("/api/snapshot")
    assert first["scenario"]["simulation"] is True
    assert first["metrics"]["provenance"] == "simulated-pos"
    assert len(first["cameras"]) == 3
    time.sleep(6)
    second = request("/api/snapshot")
    if first["scenario"]["state"] == "running":
        assert second["scenario"]["elapsedSeconds"] > first["scenario"]["elapsedSeconds"]
    print(json.dumps({
        "event": "retail_live_snapshot",
        "cameras": len(second["cameras"]),
        "transactions": second["metrics"]["transactions"],
        "visionStates": [camera["vision"]["status"] for camera in second["cameras"]],
        "elapsedSeconds": second["scenario"]["elapsedSeconds"],
        "mediaReady": second["services"]["footageReady"],
    }), flush=True)
    for camera in second["cameras"]:
        assert camera["footage"] == "synthetic-3d"
        observation = camera["vision"]
        if observation["status"] == "ready":
            assert observation["provenance"] == "model-inference"
            assert observation["people"] == len(observation["detections"])
            assert observation["frameUri"]
            assert observation["observedAt"]
        else:
            assert observation["people"] is None
        req = urllib.request.Request(base + camera["mediaUri"], headers={"Range": "bytes=0-1023"})
        with opener.open(req, timeout=60) as response:
            assert response.status == 206
            assert len(response.read()) == 1024
    if args.exercise_agent:
        run = request("/api/agents/run", "POST", {
            "agent": "merchandising",
            "question": "Analise a exposição do denim e recomende uma intervenção reversível na simulação, com evidências e limitações."
        })
        deadline = time.monotonic() + 300
        while run["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise TimeoutError("Managed Foundry agent did not finish")
            time.sleep(5)
            run = request(f"/api/agents/runs/{run['id']}")
        assert run["status"] == "completed", run.get("error")
        assert run["execution"] == "azure-foundry-agent"
        assert run["responseId"] and run["agentVersion"]
        receipts = [json.loads(value) for value in run["toolCalls"]]
        assert any(value.get("name") == "read_demo_context" and value.get("status") == "executed" for value in receipts)
        print(json.dumps({
            "event": "retail_managed_agent_completed",
            "runId": run["id"],
            "agentName": run["agentName"],
            "agentVersion": run["agentVersion"],
            "responseId": run["responseId"],
            "recommendations": len(run["recommendations"]),
        }), flush=True)


if __name__ == "__main__":
    main()
