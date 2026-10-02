"""Build CC0 anatomical, clothed Blender templates using local MPFB sources.

Run portable Blender with --background --factory-startup --python this_file.
Append HumanTemplate (adult A), HumanTemplate_B, or HumanTemplate_C.
Each collection has one root armature and six skinned meshes, in metres,
Z up, facing -Y. Copy objects AND remap parents/armature modifiers for
independent animation; collection instances share the source pose.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local" / "human-assets"
COMMIT = "afb9f530a7c2741dedb8df0ebae2e0b183caec21"
SOURCE = LOCAL / "mpfb"
PACK = LOCAL / "system-assets"
WARDROBE = LOCAL / "wardrobe"
MANIFEST = ROOT / "assets" / "generated" / "human-license-manifest.json"


def sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(path):
    return str(Path(path).relative_to(ROOT))


def bootstrap():
    import bpy

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SOURCE / "src"))
    home = LOCAL / "mpfb-user"
    home.mkdir(exist_ok=True)
    bpy.context.preferences.filepaths.temporary_directory = str(LOCAL)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.use_preferences_save = False
    import mpfb

    # Redirect the extension API before LocationService initializes; nothing is
    # installed into Blender's shared user profile.
    extension_path_user = bpy.utils.extension_path_user
    bpy.utils.extension_path_user = lambda *args, **kwargs: str(home)
    preferences = {"mpfb_user_data": str(home), "mpfb_second_root": str(PACK)}
    mpfb.get_preference = lambda name: preferences.get(name)
    try:
        mpfb.register()
    finally:
        bpy.utils.extension_path_user = extension_path_user
    return mpfb


def material(name, color, roughness=0.65):
    import bpy

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.diffuse_color = (*color, 1)
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = roughness
    return mat


def build():
    import bpy
    from mathutils import Vector

    bootstrap()
    from mpfb.services.humanservice import HumanService
    from mpfb.services.targetservice import TargetService
    from mpfb.services.exportservice import ExportService

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1
    inputs = {
        "Shirt": WARDROBE / "clothes" / "toigo_basic_tucked_t-shirt" / "toigo_basic_tucked_t-shirt.mhclo",
        "Pants": WARDROBE / "clothes" / "cortu_cargo_pants" / "cortu_cargo_pants.mhclo",
        "Shoes": PACK / "clothes" / "shoes01" / "shoes01.mhclo",
        "Eyes": PACK / "eyes" / "low-poly" / "low-poly.mhclo",
    }
    used = set(inputs.values())
    records = []
    variants = [
        ("A", 0.85, 0.50, "short02", (0.46, 0.27, 0.16), (0.06, 0.30, 0.35)),
        ("B", 0.15, 0.43, "bob01", (0.68, 0.44, 0.30), (0.58, 0.14, 0.09)),
        ("C", 0.60, 0.56, "afro01", (0.22, 0.105, 0.055), (0.24, 0.25, 0.48)),
    ]
    for index, (suffix, gender, height, hairstyle, skin_color, shirt_color) in enumerate(variants):
        collection_name = "HumanTemplate" if index == 0 else "HumanTemplate_" + suffix
        collection = bpy.data.collections.new(collection_name)
        scene.collection.children.link(collection)
        previous = set(bpy.data.objects)
        macro = TargetService.get_default_macro_info_dict()
        macro.update(gender=gender, age=0.45, height=height, weight=0.48, muscle=0.48)
        body = HumanService.create_human(macro_detail_dict=macro)
        body.name = "Human_" + suffix + "_Body"
        rig = HumanService.add_builtin_rig(body, "game_engine", import_weights=True)
        rig.name = "Human_" + suffix + "_Rig"
        rig.data.name = rig.name
        rig["human_template"] = suffix
        rig["forward_axis"] = "-Y"
        rig["units"] = "metres"
        body.data.materials.clear()
        body.data.materials.append(material("Human_" + suffix + "_Skin", skin_color))
        paths = dict(inputs)
        paths["Hair"] = PACK / "hair" / hairstyle / (hairstyle + ".mhclo")
        used.add(paths["Hair"])
        for role, path in paths.items():
            kind = role if role in ("Eyes", "Hair") else "Clothes"
            obj = HumanService.add_mhclo_asset(
                str(path), body, asset_type=kind, subdiv_levels=0,
                material_type="MAKESKIN", set_up_rigging=True,
                interpolate_weights=True,
            )
            obj.name = "Human_" + suffix + "_" + role
            obj["wardrobe_role"] = role.lower()
            if role == "Shirt":
                hem = min(vertex.co.z for vertex in obj.data.vertices)
                for vertex in obj.data.vertices:
                    influence = max(0.0, 1.0 - (vertex.co.z - hem) / 0.15)
                    vertex.co.z -= 0.055 * influence
            if role in ("Shirt", "Pants", "Shoes"):
                color = {"Shirt": shirt_color, "Pants": (0.035, 0.065, 0.13),
                         "Shoes": (0.045, 0.035, 0.028)}[role]
                obj.data.materials.clear()
                obj.data.materials.append(material("Human_" + suffix + "_" + role, color))
                for polygon in obj.data.polygons:
                    polygon.material_index = 0
        # Preserve deformation weights while permanently removing construction
        # helpers and the body areas masked by garments.
        ExportService.bake_modifiers_remove_helpers(
            body, bake_masks=True, bake_subdiv=False, remove_helpers=True,
        )
        objects = set(bpy.data.objects) - previous
        for obj in objects:
            for owner in list(obj.users_collection):
                owner.objects.unlink(obj)
            collection.objects.link(obj)
            if obj.type == "MESH":
                for polygon in obj.data.polygons:
                    polygon.use_smooth = True
                for modifier in obj.modifiers:
                    if modifier.type == "SUBSURF":
                        modifier.levels = 0
                        modifier.render_levels = 1
        # Root is at the floor; all variants are independently appendable at
        # the origin, not arranged side by side in the asset library.
        bpy.context.view_layer.update()
        minimum_z = min(
            (obj.matrix_world @ Vector(corner)).z
            for obj in objects if obj.type == "MESH" for corner in obj.bound_box
        )
        rig.location.z -= minimum_z
        for bone in rig.pose.bones:
            bone.rotation_mode = "XYZ"
        for side, sign in (("l", 1), ("r", -1)):
            rig.pose.bones["upperarm_" + side].rotation_euler.y = math.radians(sign * 55)
            rig.pose.bones["lowerarm_" + side].rotation_euler.x = math.radians(-8)
        bpy.context.view_layer.update()
        meshes = [obj for obj in objects if obj.type == "MESH"]
        assert len(meshes) == 6, (suffix, [obj.name for obj in meshes])
        for obj in meshes:
            assert any(mod.type == "ARMATURE" and mod.object == rig for mod in obj.modifiers), obj.name
            assert len(obj.vertex_groups) > 0, obj.name
        records.append({
            "collection": collection_name, "root_armature": rig.name,
            "mesh_count": len(meshes), "bone_count": len(rig.data.bones),
            "vertices": sum(len(obj.data.vertices) for obj in meshes),
            "polygons": sum(len(obj.data.polygons) for obj in meshes),
            "meshes": [{"name": obj.name, "vertices": len(obj.data.vertices),
                        "polygons": len(obj.data.polygons),
                        "materials": [slot.material.name for slot in obj.material_slots if slot.material]}
                       for obj in sorted(meshes, key=lambda ob: ob.name)],
            "bones": list(rig.data.bones.keys()),
        })

    scene.frame_start, scene.frame_end = 1, 48
    bpy.ops.file.pack_all()
    # Do not embed the authoring add-on or Python logic into the distributable.
    for text in list(bpy.data.texts):
        bpy.data.texts.remove(text)
    output = LOCAL / "humans.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=True)
    archives = [
        ("mpfb2-source.zip", "https://codeload.github.com/makehumancommunity/mpfb2/zip/" + COMMIT,
         "CC0-1.0 assets; GPL-3.0-or-later code (local authoring only)"),
        ("makehuman_system_assets_cc0.zip", "https://files2.makehumancommunity.org/asset_packs/makehuman_system_assets/makehuman_system_assets_cc0.zip", "CC0-1.0"),
        ("shirts01_cc0.zip", "https://files2.makehumancommunity.org/asset_packs/shirts01/shirts01_cc0.zip", "CC0-1.0"),
        ("pants01_cc0.zip", "https://files2.makehumancommunity.org/asset_packs/pants01/pants01_cc0.zip", "CC0-1.0"),
    ]
    license_url = "https://github.com/makehumancommunity/mpfb2/blob/" + COMMIT + "/LICENSE.md"
    files = []
    for path in sorted(used):
        files.append({"path": relative(path), "sha256": sha256(path),
                      "license": "CC0-1.0", "header": path.read_text(errors="replace").split("verts")[0]})
    evidence = [
        LOCAL / "asset-packs-license.html",
        SOURCE / "LICENSE.md", SOURCE / "LICENSE.ASSETS.md", SOURCE / "LICENSE.CODE.md",
    ]
    graphical_inputs = [
        SOURCE / "src" / "mpfb" / "data" / "3dobjs" / "base.obj",
        SOURCE / "src" / "mpfb" / "data" / "rigs" / "standard" / "rig.game_engine.json",
        SOURCE / "src" / "mpfb" / "data" / "rigs" / "standard" / "weights.game_engine.json",
    ]
    for path in sorted(used):
        graphical_inputs.extend(file for file in path.parent.iterdir()
                                if file.suffix.lower() in (".obj", ".mhmat", ".png", ".jpg", ".jpeg"))
    manifest = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset_license": "CC0-1.0", "source_commit": COMMIT,
        "license_urls": [license_url, license_url.replace("LICENSE.md", "LICENSE.ASSETS.md"),
                         "https://static.makehumancommunity.org/assets/assetpacks.html",
                         "https://creativecommons.org/publicdomain/zero/1.0/"],
        "code_policy": "MPFB GPL code and source archives remain under .local; no add-on code embedded in humans.blend.",
        "wardrobe_note": "Separate tucked T-shirt, long cargo pants colored denim-blue, closed shoes, mesh hair and eyes. Pants are cargo trousers, not a jeans asset.",
        "sources": [{"path": relative(LOCAL / name), "url": url, "sha256": sha256(LOCAL / name), "license": license}
                    for name, url, license in archives],
        "selected_assets": files,
        "license_evidence": [{"path": relative(path), "sha256": sha256(path)} for path in evidence],
        "graphical_inputs": [{"path": relative(path), "sha256": sha256(path), "license": "CC0-1.0"}
                             for path in sorted(set(graphical_inputs))],
        "output": {"path": relative(output), "sha256": sha256(output)},
        "coordinate_system": {"up": "+Z", "forward": "-Y", "units": "metres", "origin": "floor under feet",
                              "template_placement": "All variants at origin; append only one collection per actor."},
        "animation": {"rig": "MPFB game_engine", "rotation_mode": "XYZ", "left_arm": ["upperarm_l", "lowerarm_l", "hand_l"],
                      "right_arm": ["upperarm_r", "lowerarm_r", "hand_r"],
                      "left_leg": ["thigh_l", "calf_l", "foot_l"], "right_leg": ["thigh_r", "calf_r", "foot_r"],
                      "torso": ["pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "head"],
                      "root_bone": "Root", "clips": [],
                      "duplication": "Copy all collection objects and remap parents and armature modifier targets; copy armature data/action for independent animation."},
        "templates": records,
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("HUMAN_BUILD_OK", json.dumps(records))


if __name__ == "__main__":
    build()
