"""Exhaustive authoring-only checks; no detector/worker inputs are produced."""

import argparse
from array import array
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

import bpy

ROOT = Path(__file__).resolve().parents[2]
VARIANTS = ("baseline", "optimized")


def sha256_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matrix_values(matrix):
    return [value for row in matrix for value in row]


def difference(a, b):
    if len(a) != len(b) or not all(math.isfinite(x) for x in (*a, *b)):
        raise ValueError("Non-finite or mismatched actor state")
    return max((abs(x - y) for x, y in zip(a, b)), default=0.0)


def mesh_digest(ob):
    mesh = ob.data
    digest = hashlib.sha256()
    for collection, attribute, width, code in (
        (mesh.vertices, "co", 3, "f"), (mesh.edges, "vertices", 2, "i"),
        (mesh.loops, "vertex_index", 1, "i"),
        (mesh.polygons, "loop_start", 1, "i"),
        (mesh.polygons, "loop_total", 1, "i"),
        (mesh.polygons, "material_index", 1, "i"),
    ):
        values = array(code, [0]) * (len(collection) * width)
        collection.foreach_get(attribute, values)
        digest.update(values.tobytes())
    for layer in mesh.uv_layers:
        values = array("f", [0]) * (len(layer.data) * 2)
        layer.data.foreach_get("uv", values)
        digest.update(layer.name.encode())
        digest.update(values.tobytes())
    weights = [[(g.group, g.weight) for g in vertex.groups] for vertex in mesh.vertices]
    digest.update(json.dumps(weights, separators=(",", ":")).encode())
    return digest.hexdigest()


def material_signature(material):
    if material is None:
        return None
    result = {"name": material.name, "diffuse": list(material.diffuse_color),
              "useNodes": material.use_nodes}
    if material.use_nodes:
        result["nodes"] = [
            {"name": node.name, "type": node.bl_idname,
             "inputs": [(socket.name, list(socket.default_value)
                         if hasattr(socket.default_value, "__iter__")
                         and not isinstance(socket.default_value, str)
                         else socket.default_value
                         if isinstance(socket.default_value, (str, bool, int, float))
                         or socket.default_value is None
                         else str(socket.default_value))
                        for socket in node.inputs if hasattr(socket, "default_value")]}
            for node in material.node_tree.nodes
        ]
        result["links"] = [(link.from_node.name, link.from_socket.identifier,
                            link.to_node.name, link.to_socket.identifier)
                           for link in material.node_tree.links]
    return result


def identity(actors):
    return [
        (rig.as_pointer(), rig.data.as_pointer(),
         tuple((ob.as_pointer(), ob.data.as_pointer(),
                ob.parent.as_pointer() if ob.parent else None,
                tuple((mod.name, mod.type,
                       mod.object.as_pointer() if mod.type == "ARMATURE" and mod.object else None,
                       mod.show_viewport, mod.show_render) for mod in ob.modifiers),
                tuple(slot.material.as_pointer() if slot.material else None
                      for slot in ob.material_slots)) for ob in meshes))
        for rig, meshes in actors
    ]


def content(actors):
    return [
        {"rig": rig.name,
         "restBones": [(bone.name, matrix_values(bone.matrix_local)) for bone in rig.data.bones],
         "meshes": [{"object": ob.name, "mesh": ob.data.name,
                     "vertices": len(ob.data.vertices), "geometryWeightsUvSha256": mesh_digest(ob),
                     "vertexGroups": [group.name for group in ob.vertex_groups],
                     "materials": [material_signature(slot.material) for slot in ob.material_slots]}
                    for ob in meshes]}
        for rig, meshes in actors
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-endpoint-sentinels", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    started = time.perf_counter()
    spec = importlib.util.spec_from_file_location("store_authoring", Path(__file__).with_name("render_store.py"))
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    store.OUT.mkdir(parents=True, exist_ok=True)
    store.build()
    scene = bpy.context.scene
    actors = []
    for rig, *_ in store.ACTORS:
        meshes = sorted({ob for collection in rig.users_collection for ob in collection.objects
                         if ob.type == "MESH"}, key=lambda ob: ob.name)
        if not meshes or not any(mod.type == "ARMATURE" and mod.object == rig
                                 for ob in meshes for mod in ob.modifiers):
            raise ValueError(f"Missing skinned meshes for {rig.name}")
        actors.append((rig, meshes))
    expected_identity = identity(actors)
    expected_content = content(actors)
    frame_count = store.FPS * store.DURATION

    def state(frame, variant):
        store.pose(frame / store.FPS, variant)
        scene.frame_set(frame + 1)  # Match render_store's ordering, including endpoint 900.
        bpy.context.view_layer.update()
        if identity(actors) != expected_identity:
            raise ValueError(f"Actor/mesh/material identity changed at {variant}:{frame}")
        values = []
        for rig, meshes in actors:
            values += matrix_values(rig.matrix_world)
            for bone in rig.pose.bones:
                values += matrix_values(bone.matrix_basis) + matrix_values(bone.matrix)
            for ob in meshes:
                values += matrix_values(ob.matrix_world)
        return values

    baseline_states = []
    deltas = []
    for variant in VARIANTS:
        for frame in range(frame_count):
            values = state(frame, variant)
            difference(values, values)
            if variant == "baseline":
                baseline_states.append(values)
            else:
                deltas.append(difference(baseline_states[frame], values))
            if frame % 150 == 0:
                print(f"POSE {variant} {frame}/{frame_count}", flush=True)
        if content(actors) != expected_content:
            raise ValueError(f"Actor source geometry/materials changed after {variant}")

    loops = {}
    racks = {}
    for variant in VARIANTS:
        start = state(0, variant)
        last = state(frame_count - 1, variant)
        end = state(frame_count, variant)
        loops[variant] = {"endpointMaxStateDifference": difference(start, end),
                          "lastNativeToFirstMaxStateDifference": difference(last, start)}
        racks[variant] = [rack.rotation_euler.z for rack in store.RACKS]
    adjacent = [difference(a, b) for a, b in zip(baseline_states, baseline_states[1:])]
    ordered = sorted(adjacent)
    actor_report = [
        {"rig": actor["rig"], "meshes": [
            {key: value for key, value in mesh.items() if key != "materials"}
            for mesh in actor["meshes"]]}
        for actor in expected_content
    ]
    report = {
        "purpose": "Authoring geometry validation only, never an input to detector or worker",
        "validatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "blenderVersion": bpy.app.version_string,
        "renderStoreSha256": sha256_file(Path(store.__file__)),
        "humanAssetsSha256": sha256_file(ROOT / ".local" / "human-assets" / "humans.blend"),
        "validatorSha256": sha256_file(Path(__file__)),
        "timelineSamples": frame_count, "frameRange": [0, frame_count - 1],
        "fps": store.FPS, "durationSeconds": store.DURATION,
        "actors": len(actors), "actorMeshes": sum(len(meshes) for _, meshes in actors),
        "actorIdentity": actor_report,
        "actorSourceContentSha256": hashlib.sha256(
            json.dumps(expected_content, sort_keys=True).encode()).hexdigest(),
        "sharedRigMeshMaterialIdentityVerified": True,
        "sourceGeometryWeightsRestBonesMaterialsUnchanged": True,
        "maxActorPoseDifferenceBetweenVariants": max(deltas),
        "perFrameMaxActorStateDifferences": deltas,
        "loopEndpointMaxPoseDifference": max(v["endpointMaxStateDifference"] for v in loops.values()),
        "loops": loops, "physicalDenimRackRadians": racks,
        "sharedActorTimelineVerified": max(deltas) == 0,
        "closedLoopVerified": all(v["endpointMaxStateDifference"] < 1e-6 for v in loops.values()),
        "adjacentActorStateDifferences": {
            "metric": "Max absolute entry difference across rig world, bone basis/pose, mesh world matrices; not physical distance",
            "median": ordered[len(ordered) // 2], "maximum": max(adjacent),
            "largestTransitions": [{"from": i, "to": i + 1, "difference": adjacent[i]}
                                   for i in sorted(range(len(adjacent)),
                                                   key=adjacent.__getitem__, reverse=True)[:20]]},
        "limitations": [
            "Exact rig, mesh, material datablock identity is checked at every pose in one rebuilt scene, not recovered from MP4 pixels.",
            "Source geometry/weights/UVs/rest bones/materials are hashed before and after each variant; evaluated deformed vertex arrays are not exhaustively compared.",
            "Endpoint equality is not a guarantee of continuous velocity, natural gait, or perceptual smoothness; pause/walk transitions can be abrupt.",
            "Native image integrity, resume boundaries and pixel seam measurements are reported separately."
        ],
    }
    report["passed"] = (report["sharedActorTimelineVerified"] and report["closedLoopVerified"]
                        and racks["baseline"] != racks["optimized"])
    if args.render_endpoint_sentinels and report["passed"]:
        folder = store.OUT / ("endpoint-verification-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        folder.mkdir()
        records = []
        for zone in store.CENTERS:
            scene.camera = store.CAMERAS[zone]
            for variant in VARIANTS:
                for frame in (0, frame_count):
                    state(frame, variant)
                    path = folder / f"{zone}-{variant}-{frame:04d}.png"
                    scene.render.filepath = str(path)
                    bpy.ops.render.render(write_still=True)
                    records.append({"camera": zone, "variant": variant, "frame": frame,
                                    "path": str(path.relative_to(ROOT)),
                                    "fileSha256": sha256_file(path)})
        report["endpointSentinels"] = records
    report["elapsedSeconds"] = round(time.perf_counter() - started, 3)
    output = ROOT / "assets" / "generated" / "scene-validation.json"
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "passed", "timelineSamples", "actors", "actorMeshes",
        "maxActorPoseDifferenceBetweenVariants", "loops", "elapsedSeconds")}), flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
