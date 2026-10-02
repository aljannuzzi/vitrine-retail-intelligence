from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

ZONES = ("denim", "essentials", "checkout")
CAMERAS = tuple("camera-" + zone for zone in ZONES)
NAMES = {"denim": "Denim autoral", "essentials": "Essenciais", "checkout": "Caixa"}
ACTIONS = [
    {"id": "reposition-denim", "title": "Reposicionar denim",
     "description": "Alterar apenas o layout e os parâmetros programados da simulação.",
     "zoneId": "denim", "guardrails": ["Aprovação humana", "Sem efeito em loja real"]},
    {"id": "replenish-essentials", "title": "Repor tamanhos essenciais",
     "description": "Repor tamanhos no estoque sintético desta época.",
     "zoneId": "essentials", "guardrails": ["Aprovação humana", "Sem pedidos reais"]},
]
DISCLAIMER = ("Vendas, visitas e permanência são sintéticas. Mudanças são programadas; "
              "o controle é virtual e não demonstra causalidade ou resultado no mundo real.")


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def seed(now, revision=0):
    return {
        "id": "state", "pk": "store-demo", "epochId": str(uuid4()),
        "state": "running", "elapsedSeconds": 0.0, "speed": 1, "layoutVersion": 1,
        "activeSince": iso(now), "lastWall": now, "lastViewer": 0.0,
        "revision": revision, "sequence": 0, "nextStep": 5,
        "metrics": {"revenueCents": 0, "transactions": 0, "units": 0,
                    "averageBasketCents": 0, "simulatedVisits": 0,
                    "provenance": "simulated-pos"},
        "zoneTotals": {z: {"visitors": 0, "revenueCents": 0, "units": 0} for z in ZONES},
        "controlRevenue": 0, "controlLastSale": None, "posEvents": [], "series": [], "samples": [],
        "interventions": [], "agentRuns": [], "lastSale": None,
        "agentStarts": [], "runHistory": [], "comparison": None, "lastVisionRequest": 0,
        "owner": None, "leaseUntil": 0,
    }


def action_catalog(state):
    applied = {x["actionId"] for x in state["interventions"]}
    return [{**deepcopy(a), "applied": a["id"] in applied} for a in ACTIONS]


def variant(state):
    return "optimized" if state["interventions"] else "baseline"


def clock_document(state, now):
    return {"id": "clock", "pk": "store-demo", "epochId": state["epochId"],
            "state": state["state"], "elapsedSeconds": state["elapsedSeconds"],
            "speed": state["speed"], "updatedAt": iso(now),
            "activeUntil": iso(state["lastViewer"] + 120),
            "layoutVersion": state["layoutVersion"], "variant": variant(state)}


def step(state, elapsed, wall):
    index = int(elapsed // 5)
    visits = 3 + index % 4
    state["metrics"]["simulatedVisits"] += visits
    for n, z in enumerate(ZONES):
        state["zoneTotals"][z]["visitors"] += max(1, visits - n)
    applied = {i["actionId"] for i in state["interventions"]}
    sale_zone = "denim" if index % 2 else "essentials"
    base_price = 15900 if sale_zone == "denim" else 7900
    control = 0
    if index % 13 == 0 and state.get("controlLastSale"):
        control = -state["controlLastSale"]
        state["controlLastSale"] = None
    elif index % 3 == 0:
        control = base_price
        state["controlLastSale"] = control
    generated = index % 3 == 0 or (
        sale_zone == "denim" and "reposition-denim" in applied and index % 4 == 1
    ) or (sale_zone == "essentials" and "replenish-essentials" in applied and index % 5 == 0)
    revenue = transactions = 0
    if index % 13 == 0 and state["lastSale"]:
        prior = state.pop("lastSale")
        state["lastSale"] = None
        event = {**prior, "units": -prior["units"], "amountCents": -prior["amountCents"],
                 "type": "return"}
    elif generated:
        event = {"sku": "VT-D01" if sale_zone == "denim" else "VT-E01",
                 "item": NAMES[sale_zone], "zoneId": sale_zone, "size": "M",
                 "units": 1, "amountCents": base_price, "type": "sale"}
        state["lastSale"] = deepcopy(event)
    else:
        event = None
    if event:
        state["sequence"] += 1
        event.update(id=f'{state["epochId"]}:pos:{state["sequence"]}',
                     at=iso(wall), elapsedSeconds=elapsed, provenance="simulated-pos")
        revenue = event["amountCents"]
        transactions = int(event["type"] == "sale")
        state["posEvents"].append(event)
        state["posEvents"] = state["posEvents"][-100:]
        for target in (state["metrics"], state["zoneTotals"][event["zoneId"]]):
            target["revenueCents"] += revenue
            target["units"] += event["units"]
        state["metrics"]["transactions"] += transactions
    state["controlRevenue"] += control
    metrics = state["metrics"]
    metrics["averageBasketCents"] = (
        round(metrics["revenueCents"] / metrics["transactions"]) if metrics["transactions"] else 0)
    state["samples"].append({"second": elapsed, "visits": visits, "revenue": revenue,
                             "control": control, "transactions": transactions})
    state["samples"] = state["samples"][-720:]
    state["series"].append({
        "at": iso(wall), "elapsedSeconds": elapsed, "revenueCents": metrics["revenueCents"],
        "transactions": metrics["transactions"], "controlRevenueCents": state["controlRevenue"],
        "visits": metrics["simulatedVisits"]})
    state["series"] = state["series"][-240:]
    if state["interventions"] and state.get("comparison") is None:
        result = experiment(state, elapsed=elapsed)
        if result["status"] == "simulated-comparison":
            state["comparison"] = result


def advance(state, now):
    anchor = max(state["lastWall"], now)
    active_end = min(now, state["lastViewer"] + 120)
    delta = max(0, active_end - state["lastWall"])
    if state["state"] == "running" and state["lastViewer"] > 0:
        old_elapsed = state["elapsedSeconds"]
        state["elapsedSeconds"] += delta * state["speed"]
        while state["nextStep"] <= state["elapsedSeconds"]:
            event_wall = state["lastWall"] + (state["nextStep"] - old_elapsed) / state["speed"]
            step(state, state["nextStep"], event_wall)
            state["nextStep"] += 5
    if state["lastViewer"] > 0 and now - state["lastViewer"] > 300:
        state["state"] = "paused"
    state["lastWall"] = anchor


def experiment(state, elapsed=None):
    if state.get("comparison"):
        return deepcopy(state["comparison"])
    result = {"status": "not-started", "interventionAt": None,
              "beforeRevenuePer100Visits": None, "afterRevenuePer100Visits": None,
              "controlChangePercent": None, "relativeChangePercent": None,
              "beforeTransactions": 0, "afterTransactions": 0, "caveat": DISCLAIMER}
    if not state["interventions"]:
        return result
    intervention = state["interventions"][-1]
    start = intervention["elapsedSeconds"]
    result.update(status="collecting", interventionAt=intervention["at"])
    before = [s for s in state["samples"] if start - 60 < s["second"] <= start]
    after = [s for s in state["samples"] if start < s["second"] <= start + 60]
    result["beforeTransactions"] = sum(s["transactions"] for s in before)
    result["afterTransactions"] = sum(s["transactions"] for s in after)
    # Equal complete windows and positive visit/control denominators are mandatory.
    current = state["elapsedSeconds"] if elapsed is None else elapsed
    if start < 60 or current < start + 60 or len(before) != 12 or len(after) != 12:
        return result
    totals = [{key: sum(s[key] for s in samples)
               for key in ("visits", "revenue", "control")} for samples in (before, after)]
    b, a = totals
    if min(b["visits"], a["visits"]) < 20 or min(b["revenue"], b["control"], a["control"]) <= 0:
        return result
    b_rate, a_rate = b["revenue"] / b["visits"], a["revenue"] / a["visits"]
    control_ratio = (a["control"] / a["visits"]) / (b["control"] / b["visits"])
    result.update(status="simulated-comparison",
                  beforeRevenuePer100Visits=round(b_rate * 100, 2),
                  afterRevenuePer100Visits=round(a_rate * 100, 2),
                  controlChangePercent=round((control_ratio - 1) * 100, 2),
                  relativeChangePercent=round(((a_rate / b_rate) / control_ratio - 1) * 100, 2))
    result["caveat"] += (" Janelas iguais de 60 segundos simulados; receita líquida em centavos "
                        "por 100 visitas simuladas. Controle virtual usa o mesmo fluxo de visitas. "
                        "Comparação da intervenção mais recente; efeitos anteriores podem permanecer.")
    return result


def unavailable_vision(camera, status="unavailable"):
    return {"id": f"{camera}:unavailable", "cameraId": camera, "status": status,
            "observedAt": None, "sourceSeconds": None, "people": None, "detections": [],
            "frameUri": None, "latencyMs": None, "model": "RT-DETRv2",
            "modelRevision": "unavailable", "provenance": "model-inference",
            "error": "Inferência real ainda indisponível."}


def snapshot(state, now, observations=None, foundry=False, persistence="local", footage=False):
    observations = observations or {}
    actions = action_catalog(state)
    applied = {a["id"] for a in actions if a["applied"]}
    cameras = [{
        "id": camera, "name": NAMES[zone], "zoneId": zone,
        "mediaUri": f"/api/media/{camera}/{variant(state)}.mp4",
        "variant": variant(state), "durationSeconds": 60,
        "sourceSeconds": round(state["elapsedSeconds"] % 60, 3), "footage": "synthetic-3d",
        "vision": observations.get(camera, unavailable_vision(camera))}
        for camera, zone in zip(CAMERAS, ZONES)]
    statuses = [camera["vision"]["status"] for camera in cameras]
    return {
        "serverTime": iso(now), "revision": state["revision"],
        "store": {"id": "store-demo", "name": "VITRINE / Loja conceito", "areaM2": 180,
                  "description": "Loja ficcional original; demonstração em nuvem com dados sintéticos."},
        "scenario": {"id": state["epochId"], "state": state["state"],
                     "elapsedSeconds": round(state["elapsedSeconds"], 3), "speed": state["speed"],
                     "layoutVersion": state["layoutVersion"], "simulation": True,
                     "activeSince": state["activeSince"], "label": "Simulação programada"},
        "metrics": deepcopy(state["metrics"]),
        "zones": [{**state["zoneTotals"][z], "id": z, "name": NAMES[z],
                   "collection": "Coleção ficcional VITRINE", "colorRole": color,
                   "dwellSeconds": 35 + n * 15, "availableSizes": (
                       ["P", "M", "G", "GG"] if z != "essentials" or
                       "replenish-essentials" in applied else ["P", "M"]),
                   "stockStatus": ("replenished" if z == "essentials" and
                                   "replenish-essentials" in applied else
                                   "limited" if z == "essentials" else "available"),
                   "observation": "Visitas, permanência e estoque explicitamente simulados.",
                   "provenance": "simulation"}
                  for n, (z, color) in enumerate(zip(ZONES, ("accent", "link", "success")))],
        "cameras": cameras, "posEvents": list(reversed(deepcopy(state["posEvents"]))),
        "series": deepcopy(state["series"]), "availableActions": actions,
        "interventions": deepcopy(state["interventions"]),
        "agentRuns": [public_run(r) for r in reversed(state["agentRuns"])],
        "experiment": experiment(state),
        "services": {"foundry": "configured" if foundry else "unavailable",
                     "vision": "ready" if all(s == "ready" for s in statuses) else
                     "warming" if "warming" in statuses else "unavailable",
                     "persistence": persistence, "footageReady": footage},
        "notices": [DISCLAIMER,
                    "Detecções referem-se apenas ao frame e segundo informados, não ao vídeo em movimento.",
                    "Nenhuma venda simulada é atribuída a uma pessoa detectada.",
                    "Históricos são limitados; os totais incluem eventos anteriores ao histórico visível."],
    }


def public_run(run):
    return deepcopy({k: v for k, v in run.items() if not k.startswith("_")})
