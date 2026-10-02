"""Original store authoring, not part of the cloud runtime.

Portable Blender: --background --factory-startup --python this_file -- --pilot
Render animation: same command with --camera denim --variant baseline
"""

import argparse
import json
import math
import os
from pathlib import Path
import random
import sys
import time

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".local" / "renders"
FPS = 15
DURATION = 60
CENTERS = {"denim": -6.4, "essentials": 0.0, "checkout": 6.4}
ACTORS = []
RACKS = []
CAMERAS = {}
random.seed(712)


def material(name, color, metallic=0, roughness=.6, texture=False):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    if texture:
        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 65
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = .17
        bump.inputs["Distance"].default_value = .018
        mat.node_tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        mat.node_tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


def cube(name, pos, scale, mat, bevel=.025, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=pos)
    ob = bpy.context.object
    ob.name = name
    ob.dimensions = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if mat:
        ob.data.materials.append(mat)
    if bevel:
        mod = ob.modifiers.new("Soft manufactured edges", "BEVEL")
        mod.width, mod.segments = bevel, 2
        ob.modifiers.new("Weighted corner normals", "WEIGHTED_NORMAL")
    if parent:
        ob.parent = parent
    return ob


def rod(name, a, b, radius, mat, parent=None):
    delta = Vector(b) - Vector(a)
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=radius, depth=delta.length,
                                      location=(Vector(a) + Vector(b)) / 2)
    ob = bpy.context.object
    ob.name = name
    ob.rotation_euler = delta.to_track_quat("Z", "Y").to_euler()
    ob.data.materials.append(mat)
    if parent:
        ob.parent = parent
    return ob


def label(text, pos, size, mat):
    curve = bpy.data.curves.new(text, "FONT")
    curve.body = text
    curve.size = size
    curve.extrude = .001
    curve.align_x = "CENTER"
    ob = bpy.data.objects.new("Lettering " + text, curve)
    bpy.context.collection.objects.link(ob)
    ob.location = pos
    ob.rotation_euler = (math.pi / 2, 0, 0)
    ob.data.materials.append(mat)
    return ob


def light(name, pos, target, energy, color, size):
    data = bpy.data.lights.new(name, "AREA")
    data.energy, data.color, data.shape, data.size = energy, color, "DISK", size
    ob = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(ob)
    ob.location = pos
    ob.rotation_euler = (Vector(target) - ob.location).to_track_quat("-Z", "Y").to_euler()


def garment(parent, x, y, z, mat, jeans=False):
    # Closed, extruded cloth silhouettes: trousers have two separate legs;
    # shirts have shoulder, sleeve, neckline and hem geometry.
    if jeans:
        outline = [(-.20, 0), (.20, 0), (.19, -.88), (.035, -.88),
                   (0, -.31), (-.035, -.88), (-.19, -.88)]
    else:
        outline = [(-.12, 0), (-.27, -.07), (-.39, -.26), (-.25, -.34),
                   (-.19, -.24), (-.20, -.62), (.20, -.62), (.19, -.24),
                   (.25, -.34), (.39, -.26), (.27, -.07), (.12, 0), (0, -.055)]
    verts = [(x + px, y + depth, z + pz) for depth in (-.018, .018) for px, pz in outline]
    n = len(outline)
    faces = [tuple(range(n - 1, -1, -1)), tuple(range(n, 2 * n))]
    faces += [(i, (i + 1) % n, (i + 1) % n + n, i + n) for i in range(n)]
    mesh = bpy.data.meshes.new("Original garment pattern")
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(mat)
    ob = bpy.data.objects.new("Denim trousers" if jeans else "Cotton shirt", mesh)
    bpy.context.collection.objects.link(ob)
    ob.parent = parent
    bevel = ob.modifiers.new("Rounded cloth edges", "BEVEL")
    bevel.width, bevel.segments = .014, 2
    for face in mesh.polygons:
        face.use_smooth = True
    return ob


def rack(x, y, angle, denim=True):
    root = bpy.data.objects.new("Movable denim display" if denim else "Essentials rail", None)
    bpy.context.collection.objects.link(root)
    for side in (-1, 1):
        rod("Brass rail upright", (side * 1.15, 0, .08), (side * 1.15, 0, 1.95), .032, BRASS, root)
        rod("Stable display feet", (side * 1.15, -.32, .055), (side * 1.15, .32, .055), .038, DARK, root)
    rod("Brushed rail", (-1.15, 0, 1.95), (1.15, 0, 1.95), .032, BRASS, root)
    cube("Lower timber shelf", (0, 0, .14), (2.5, .65, .12), WOOD, parent=root)
    for i in range(9):
        px = -1.0 + i * .25
        # Hangers and garments lie across the rail rather than billboard-flat.
        hanger = bpy.data.objects.new("Hanger group", None)
        bpy.context.collection.objects.link(hanger)
        hanger.parent = root
        hanger.location = (px, 0, 0)
        hanger.rotation_euler.z = math.radians(58)
        rod("Hanger neck", (0, 0, 1.95), (0, 0, 1.83), .008, DARK, hanger)
        rod("Hanger shoulder", (-.23, 0, 1.71), (0, 0, 1.83), .012, WOOD, hanger)
        rod("Hanger shoulder", (0, 0, 1.83), (.23, 0, 1.71), .012, WOOD, hanger)
        garment(hanger, 0, 0, 1.7, DENIMS[i % 3] if denim else COTTONS[i % 4], denim)
    root.location, root.rotation_euler.z = (x, y, 0), angle
    return root


def shelves(x, denim):
    for side in (-1, 1):
        cube("Wall shelf bracket", (x + side * 1.55, 5.65, 1.55), (.055, .6, 2.7), BRASS)
    for z in (.45, 1.1, 1.75, 2.4):
        cube("Floating oak shelf", (x, 5.58, z), (3.25, .75, .065), WOOD)
        for column in range(5):
            for layer in range(3):
                cube("Folded apparel", (x - 1.25 + column * .62, 5.49, z + .06 + layer * .055),
                     (.48, .4, .052), DENIMS[(column + layer) % 3] if denim else COTTONS[(column + layer) % 4], .023)


def human(index, template, zone, offset):
    name = ["HumanTemplate", "HumanTemplate_B", "HumanTemplate_C"][template]
    with bpy.data.libraries.load(str(ROOT / ".local" / "human-assets" / "humans.blend"), link=False) as (src, dst):
        dst.collections = [name]
    collection = dst.collections[0]
    bpy.context.scene.collection.children.link(collection)
    rig = next(ob for ob in collection.objects if ob.type == "ARMATURE")
    rig.name = "Anonymous adult %02d" % index
    rig.animation_data_clear()
    for ob in collection.objects:
        if ob.type == "MESH":
            for mod in list(ob.modifiers):
                if mod.type == "SUBSURF":
                    ob.modifiers.remove(mod)
            if ob.get("wardrobe_role") == "shirt":
                mat = ob.data.materials[0].copy()
                color = [(0.035, .22, .24), (.65, .25, .10), (.65, .61, .46),
                         (.22, .30, .48), (.35, .10, .12), (.20, .32, .20)][index % 6]
                mat.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = (*color, 1)
                ob.data.materials[0] = mat
    ACTORS.append((rig, CENTERS[zone], offset, index, rig.location.z))


def build():
    global WOOD, BRASS, DARK, DENIMS, COTTONS
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES" if False else "BLENDER_EEVEE_NEXT"
    scene.eevee.taa_render_samples = 12
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    scene.render.resolution_percentage = 100
    scene.render.fps = FPS
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.compression = 15
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = -.7
    scene.world.color = (.24, .24, .24)
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 6
    bpy.context.preferences.filepaths.temporary_directory = str(OUT)
    bpy.context.preferences.filepaths.save_version = 0
    WOOD = material("Warm natural oak", (.38, .20, .085), texture=True)
    BRASS = material("Satin champagne metal", (.5, .36, .16), .7, .3)
    DARK = material("Graphite fixtures", (.025, .035, .040), .25, .4)
    plaster = material("Warm mineral plaster", (.52, .46, .37), texture=True)
    floor = material("Microcement", (.22, .24, .24), .05, .65, True)
    white = material("Soft ivory", (.80, .79, .68))
    teal = material("Teal inset", (.025, .13, .145))
    terracotta = material("Clay inset", (.29, .095, .055))
    DENIMS = [material("Indigo weave %d" % i, c, texture=True) for i, c in enumerate(
        [(.025, .055, .12), (.055, .105, .19), (.08, .14, .25)])]
    COTTONS = [material("Cotton weave %d" % i, c, texture=True) for i, c in enumerate(
        [(.66, .62, .49), (.4, .19, .11), (.13, .25, .25), (.19, .22, .33)])]
    cube("Continuous store floor", (0, 1, -.13), (22, 14, .22), floor)
    for x in range(-10, 12, 2):
        cube("Floor expansion joint", (x, 1, -.013), (.012, 14, .004), DARK, 0)
    for y in range(-5, 9, 2):
        cube("Floor expansion joint", (0, y, -.013), (22, .012, .004), DARK, 0)
    cube("Back wall", (0, 6.25, 2.25), (22, .18, 4.5), plaster)
    cube("Left return wall", (-10.9, 1, 2.25), (.18, 10.5, 4.5), plaster)
    cube("Right return wall", (10.9, 1, 2.25), (.18, 10.5, 4.5), plaster)
    for zone, x in CENTERS.items():
        cube(zone + " inset", (x, 6.1, 2.0), (5.8, .12, 3.95), teal if zone != "checkout" else terracotta)
        label(zone.upper(), (x, 5.995, 3.36), .39, white)
        label({"denim": "INDIGO / EVERYDAY", "essentials": "FORM / TEXTURE", "checkout": "THANK YOU / SEE YOU SOON"}[zone],
              (x, 5.989, 3.04), .105, white)
        cube("Woven zone rug", (x, 1.4, .002), (5.25, 5.6, .02), material(zone + " rug", (.29, .26, .21), texture=True), .01)
        light("Soft ceiling panel", (x, .5, 6.0), (x, 2, 0), 1600, (1, .87, .71), 5)
        light("Front fill", (x + 2, -4, 4), (x, 2, 1), 1100, (.77, .86, 1), 4)
        light("Back wallwash", (x, 4, 3.8), (x, 5.5, 1.6), 500, (1, .76, .48), 2)
        for dx in (-2.35, 2.35):
            rod("Ceiling track", (x + dx, -.3, 4.0), (x + dx, 5.4, 4.0), .025, DARK)
            for y in (.7, 2.7, 4.7):
                rod("Spot housing", (x + dx, y, 3.93), (x + dx, y, 3.74), .09, DARK)
        if zone != "checkout":
            shelves(x, zone == "denim")
            display = rack(x - 1.5, 3.9, math.radians(12), zone == "denim")
            if zone == "denim":
                RACKS.append(display)
            rack(x + 2.0, 3.6, math.pi / 2, zone == "denim")
            cube("Low display table", (x - 1.65, -.45, .60), (1.35, .9, .12), WOOD)
            for dx in (-.48, .48):
                cube("Table foot", (x - 1.65 + dx, -.45, .30), (.07, .65, .55), DARK)
            for layer in range(4):
                cube("Display folded stack", (x - 1.65, -.45, .69 + layer * .055), (.6, .42, .05),
                     DENIMS[layer % 3] if zone == "denim" else COTTONS[layer % 4])
        else:
            cube("Checkout oak counter", (x + .6, 4.4, .55), (3.8, .95, 1.1), WOOD, .07)
            cube("Stone counter surface", (x + .6, 4.4, 1.13), (3.95, 1.10, .10), white, .035)
            cube("Terminal stand", (x + .8, 4.1, 1.30), (.08, .10, .28), DARK)
            screen = cube("Payment terminal", (x + .8, 4.08, 1.48), (.48, .065, .32), DARK)
            screen.rotation_euler.x = math.radians(12)
            for dx in (-1.2, -.7):
                cube("Unbranded shopping bag", (x + dx, 4.35, 1.42), (.35, .25, .47), white)
                rod("Paper handle", (x + dx - .08, 4.35, 1.65), (x + dx - .08, 4.35, 1.78), .012, WOOD)
                rod("Paper handle", (x + dx + .08, 4.35, 1.65), (x + dx + .08, 4.35, 1.78), .012, WOOD)
                rod("Paper handle", (x + dx - .08, 4.35, 1.78), (x + dx + .08, 4.35, 1.78), .012, WOOD)
        for dx in (-2.7,):
            bpy.ops.mesh.primitive_cone_add(vertices=24, radius1=.25, radius2=.32, depth=.58,
                                            location=(x + dx, 5.0, .29))
            bpy.context.object.data.materials.append(terracotta)
            for i in range(9):
                a = i * 2.399
                end = (x + dx + .38 * math.cos(a), 5 + .38 * math.sin(a), 1 + .4 * (i % 3))
                rod("Plant stem", (x + dx, 5, .5), end, .013, teal)
                bpy.ops.mesh.primitive_uv_sphere_add(segments=8, ring_count=4, radius=1, location=end)
                leaf = bpy.context.object
                leaf.name, leaf.scale = "Original foliage", (.12, .24, .045)
                leaf.rotation_euler = (.3, .5, a)
                leaf.data.materials.append(teal)
        data = bpy.data.cameras.new("Oblique CCTV " + zone)
        cam = bpy.data.objects.new("camera-" + zone, data)
        bpy.context.collection.objects.link(cam)
        cam.location = (x + 3.6, -7.2, 4.1)
        target = Vector((x, 2.25, 1.25))
        cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
        data.lens = 37
        data.clip_end = 100
        CAMERAS[zone] = cam
        for j in range(3):
            human(len(ACTORS), (j + list(CENTERS).index(zone)) % 3, zone, j)


def pose(seconds, variant):
    for rack_ob in RACKS:
        rack_ob.rotation_euler.z = math.radians(12 if variant == "baseline" else 95)
    for rig, center, offset, index, base_z in ACTORS:
        # A 60-second closed out-and-back walk, with a stop/reach at each end.
        phase = (seconds + offset * 13 + (index // 3) * 3) % 60
        if phase < 20:
            fraction, movement, direction = phase / 20, 1, 1
        elif phase < 30:
            fraction, movement, direction = 1, 0, 1
        elif phase < 50:
            fraction, movement, direction = 1 - (phase - 30) / 20, 1, -1
        else:
            fraction, movement, direction = 0, 0, -1
        lane = [-.6, .65, 1.65][offset]
        y0, distance = [(-1.25, 3.9), (-.65, 3.6), (.3, 2.6)][offset]
        rig.location = (center + lane, y0 + distance * fraction, base_z)
        # Turn during pauses, smoothly, so the loop's pose is continuous.
        if 20 <= phase < 30:
            turn = min(1, max(0, (phase - 26) / 4))
            facing = math.pi * (1 - turn)
        elif phase >= 50:
            turn = min(1, max(0, (phase - 56) / 4))
            facing = math.pi * turn
        else:
            facing = math.pi if direction == 1 else 0
        rig.rotation_euler.z = facing
        step = math.sin(seconds * math.tau * .8 + index) * movement
        for side, sign in (("l", 1), ("r", -1)):
            rig.pose.bones["thigh_" + side].rotation_euler.x = .27 * step * sign
            rig.pose.bones["calf_" + side].rotation_euler.x = max(0, -.36 * step * sign)
            rig.pose.bones["foot_" + side].rotation_euler.x = -.08 * step * sign
            upper = rig.pose.bones["upperarm_" + side]
            upper.rotation_euler = (.20 * step * -sign, math.radians(sign * 82), 0)
            rig.pose.bones["lowerarm_" + side].rotation_euler.x = -.12
        reach = math.sin(math.pi * (phase - 20) / 6) if 20 < phase < 26 else 0
        rig.pose.bones["upperarm_r"].rotation_euler.x -= .7 * reach
        rig.pose.bones["lowerarm_r"].rotation_euler.x -= .5 * reach
    bpy.context.view_layer.update()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--camera", choices=list(CENTERS), default="denim")
    parser.add_argument("--variant", choices=["baseline", "optimized"], default="baseline")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=900)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    OUT.mkdir(parents=True, exist_ok=True)
    # Concurrent authoring jobs can safely resume the same clip, but must never
    # observe another renderer's partially written PNG.
    lock = None
    if not args.pilot:
        lock = (OUT / f"{args.camera}-{args.variant}.lock").open("a+b")
        if lock.tell() == 0:
            lock.write(b"1")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(2)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
    build()
    scene = bpy.context.scene
    started = time.perf_counter()
    records = []
    jobs = [(zone, variant, sec) for zone in CENTERS for variant in ("baseline", "optimized")
            for sec in (0, 15, 30, 45)] if args.pilot else [
                (args.camera, args.variant, frame / FPS) for frame in range(args.start, args.end)]
    for zone, variant, sec in jobs:
        frame = round(sec * FPS)
        folder = OUT / ("pilot" if args.pilot else f"camera-{zone}-{variant}")
        folder.mkdir(exist_ok=True)
        path = folder / (f"{zone}-{variant}-{frame:04d}.png" if args.pilot else f"{frame:04d}.png")
        scene.camera = CAMERAS[zone]
        pose(sec, variant)
        scene.frame_set(frame + 1)
        if not path.exists():
            scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
        records.append({"cameraId": "camera-" + zone, "variant": variant,
                        "sourceSeconds": sec, "file": str(path.relative_to(ROOT))})
    if args.pilot:
        (OUT / "pilot" / "frames.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(json.dumps({"rendered": len(jobs), "elapsedSeconds": time.perf_counter() - started}), flush=True)
    if lock is not None:
        lock.close()


if __name__ == "__main__":
    main()
