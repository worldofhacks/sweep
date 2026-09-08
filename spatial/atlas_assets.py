"""Bounded, self-contained Atlas assets; no remote resources or invented geometry."""

import base64
import hashlib
import json
import struct
from pathlib import Path

import cv2
import numpy as np

MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_FACES = 200_000
MAX_VERTICES = 600_000
MAX_TEXTURE_PIXELS = 16_777_216


def write_glb(path: Path, document: dict, binary: bytes) -> str:
    encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    encoded += b" " * (-len(encoded) % 4)
    binary += b"\0" * (-len(binary) % 4)
    payload = struct.pack("<4sII", b"glTF", 2, 28 + len(encoded) + len(binary))
    payload += struct.pack("<I4s", len(encoded), b"JSON") + encoded
    payload += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    if len(payload) > MAX_ASSET_BYTES:
        raise ValueError("The reconstructed asset exceeds the phone viewer's 16 MB limit.")
    staged = path.with_suffix(".pending")
    staged.write_bytes(payload)
    staged.replace(path)
    return hashlib.sha256(payload).hexdigest()


def read_glb(path: Path) -> tuple[dict, bytes]:
    if not 28 <= path.stat().st_size <= MAX_ASSET_BYTES:
        raise ValueError("The reconstructed asset has an invalid size.")
    return decode_glb(path.read_bytes())


def decode_glb(data: bytes) -> tuple[dict, bytes]:
    if not 28 <= len(data) <= MAX_ASSET_BYTES:
        raise ValueError("The reconstructed asset has an invalid size.")
    if struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
        raise ValueError("The reconstructed asset has an invalid GLB header.")
    size, kind = struct.unpack_from("<I4s", data, 12)
    if kind != b"JSON" or size % 4 or size > len(data) - 28:
        raise ValueError("The reconstructed asset has an invalid JSON chunk.")
    document = json.loads(data[20 : 20 + size])
    if not isinstance(document, dict):
        raise ValueError("The reconstructed asset has invalid metadata.")
    length, kind = struct.unpack_from("<I4s", data, 20 + size)
    if kind != b"BIN\0" or length % 4 or 28 + size + length != len(data):
        raise ValueError("The reconstructed asset has an invalid binary chunk.")
    return document, data[28 + size :]


def mesh_stats(document: dict, binary: bytes) -> dict:
    """Validate the narrow static triangle profile emitted by the pinned dense engine."""
    def integer(value, maximum):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("The mesh contains an invalid buffer reference or count.")
        return value

    def self_contained(value, depth=0):
        if depth > 32:
            raise ValueError("The mesh metadata is too deeply nested.")
        if isinstance(value, dict):
            if "uri" in value:
                raise ValueError("The mesh contains an external resource.")
            if set(value.get("extensions", {})) - {"KHR_materials_unlit"}:
                raise ValueError("The mesh contains an unsupported extension.")
            for child in value.values():
                self_contained(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                self_contained(child, depth + 1)

    self_contained(document)

    if document.get("asset", {}).get("version") != "2.0":
        raise ValueError("The mesh is not glTF 2.0.")
    if any(document.get(key) for key in ("animations", "skins", "cameras", "extensions")):
        raise ValueError("Only static reconstructed meshes are supported.")
    extensions = document.get("extensionsUsed", []) + document.get("extensionsRequired", [])
    if any(value != "KHR_materials_unlit" for value in extensions):
        raise ValueError("The mesh requests an unsupported extension.")
    buffers = document.get("buffers", [])
    if len(buffers) != 1 or "uri" in buffers[0] or buffers[0].get("byteLength") != len(binary):
        raise ValueError("The mesh must contain one embedded buffer.")
    views = document.get("bufferViews", [])
    accessors = document.get("accessors", [])
    if not 1 <= len(views) <= 64 or not 1 <= len(accessors) <= 64:
        raise ValueError("The mesh has too many or missing buffers.")
    for view in views:
        if view.get("buffer") != 0 or view.get("extensions"):
            raise ValueError("The mesh references an unsupported buffer.")
        offset = integer(view.get("byteOffset", 0), len(binary))
        length = integer(view.get("byteLength"), len(binary))
        if offset + length > len(binary):
            raise ValueError("The mesh references bytes outside its buffer.")

    def values(index, shape, allowed_types):
        accessor = accessors[integer(index, len(accessors) - 1)]
        if accessor.get("type") != shape or accessor.get("componentType") not in allowed_types:
            raise ValueError("The mesh has an unsupported vertex or index format.")
        if accessor.get("sparse") or accessor.get("normalized") or accessor.get("extensions"):
            raise ValueError("The mesh uses unsupported accessor encoding.")
        count = integer(accessor.get("count"), MAX_VERTICES)
        view = views[integer(accessor.get("bufferView"), len(views) - 1)]
        dtype = np.dtype({5126: "<f4", 5125: "<u4", 5123: "<u2"}[accessor["componentType"]])
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[shape]
        stride = view.get("byteStride", width * dtype.itemsize)
        if stride != width * dtype.itemsize:
            raise ValueError("The mesh uses an unsupported interleaved buffer.")
        offset = integer(accessor.get("byteOffset", 0), view["byteLength"])
        if count == 0 or offset + count * stride > view["byteLength"]:
            raise ValueError("The mesh accessor exceeds its buffer.")
        result = np.frombuffer(binary, dtype, count * width,
                               view.get("byteOffset", 0) + offset).reshape(count, width)
        if not np.isfinite(result).all():
            raise ValueError("The mesh contains nonfinite geometry.")
        return result

    meshes = document.get("meshes", [])
    nodes = document.get("nodes", [])
    if len(meshes) != 1 or len(nodes) != 1 or nodes[0].get("mesh") != 0:
        raise ValueError("The mesh must have one static scene node.")
    if set(nodes[0]) - {"mesh", "name"}:
        raise ValueError("The mesh has unexpected transforms or scene links.")
    if document.get("scene", 0) != 0 or len(document.get("scenes", [])) != 1:
        raise ValueError("The mesh has an invalid scene.")
    if document["scenes"][0].get("nodes") != [0]:
        raise ValueError("The mesh has invalid scene links.")
    primitives = meshes[0].get("primitives", [])
    if not 1 <= len(primitives) <= 8:
        raise ValueError("The mesh has too many or missing texture groups.")
    images, textures = document.get("images", []), document.get("textures", [])
    materials, samplers = document.get("materials", []), document.get("samplers", [])
    if not all(1 <= len(items) <= 8 for items in (images, textures, materials)):
        raise ValueError("The mesh has missing or excessive texture resources.")
    if len(samplers) > 8:
        raise ValueError("The mesh has too many texture samplers.")
    pixels = 0
    for image in images:
        view = views[integer(image.get("bufferView"), len(views) - 1)]
        start = view.get("byteOffset", 0)
        data = binary[start:start + view["byteLength"]]
        if image.get("mimeType") != "image/png" or len(data) < 33:
            raise ValueError("The mesh texture must be an embedded PNG.")
        if data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise ValueError("The mesh texture has an invalid PNG header.")
        width, height = struct.unpack_from(">II", data, 16)
        pixels += width * height
        if not 1 <= width <= 4096 or not 1 <= height <= 4096 or pixels > MAX_TEXTURE_PIXELS:
            raise ValueError("The mesh texture exceeds its pixel budget.")
    for texture in textures:
        integer(texture.get("source"), len(images) - 1)
        if "sampler" in texture:
            integer(texture["sampler"], len(samplers) - 1)
    for material in materials:
        if "KHR_materials_unlit" not in material.get("extensions", {}):
            raise ValueError("Only unlit photo-textured surfaces are supported.")
        base = material.get("pbrMetallicRoughness", {}).get("baseColorTexture", {})
        integer(base.get("index"), len(textures) - 1)
        if base.get("texCoord", 0) != 0:
            raise ValueError("The mesh references missing texture coordinates.")
    vertices, faces = 0, 0
    for primitive in primitives:
        attributes = primitive.get("attributes", {})
        if primitive.get("mode", 4) != 4 or primitive.get("targets") or primitive.get("extensions"):
            raise ValueError("Only reconstructed triangles are supported.")
        if set(attributes) - {"POSITION", "TEXCOORD_0", "NORMAL"}:
            raise ValueError("The mesh has unsupported vertex attributes.")
        integer(primitive.get("material"), len(materials) - 1)
        if "TEXCOORD_0" not in attributes:
            raise ValueError("The mesh is missing its photo texture coordinates.")
        xyz = values(attributes.get("POSITION"), "VEC3", (5126,))
        indices = values(primitive.get("indices"), "SCALAR", (5123, 5125))
        if len(indices) % 3 or indices.max() >= len(xyz):
            raise ValueError("The mesh references missing vertices.")
        for name, shape in (("TEXCOORD_0", "VEC2"), ("NORMAL", "VEC3")):
            if name in attributes and len(values(attributes[name], shape, (5126,))) != len(xyz):
                raise ValueError("The mesh attribute counts do not match.")
        vertices += len(xyz)
        faces += len(indices) // 3
    if vertices > MAX_VERTICES or faces > MAX_FACES or faces < 50:
        raise ValueError("The reconstructed mesh exceeds its geometry limits or is too small.")
    return {"vertices": vertices, "faces": faces}


def pack_openmvs_glb(source: Path, destination: Path) -> dict:
    """Embed local engine textures and data buffers without changing observed geometry."""
    document, original = read_glb(source)
    buffers = document.get("buffers", [])
    if not 1 <= len(buffers) <= 8:
        raise ValueError("The engine exported too many or missing buffers.")
    binary = bytearray()
    offsets = []
    for index, buffer in enumerate(buffers):
        uri = buffer.get("uri")
        if index == 0 and uri is None:
            data = original
        elif isinstance(uri, str) and uri.startswith("data:application/octet-stream;base64,"):
            data = base64.b64decode(uri.split(",", 1)[1], validate=True)
        else:
            raise ValueError("The engine exported an external geometry buffer.")
        if len(data) != buffer.get("byteLength") or len(binary) + len(data) > MAX_ASSET_BYTES:
            raise ValueError("The engine exported invalid buffer lengths.")
        offsets.append(len(binary))
        binary.extend(data)
        binary.extend(b"\0" * (-len(binary) % 4))
    for view in document.get("bufferViews", []):
        index = view.get("buffer")
        if type(index) is not int or not 0 <= index < len(offsets):
            raise ValueError("The engine exported an invalid buffer index.")
        offset, length = view.get("byteOffset", 0), view.get("byteLength")
        if (type(offset) is not int or type(length) is not int or min(offset, length) < 0
                or offset + length > buffers[index]["byteLength"]):
            raise ValueError("The engine exported an invalid buffer view.")
        view.update(buffer=0, byteOffset=offsets[index] + offset)
    pixels = 0
    images = document.get("images", [])
    if not 1 <= len(images) <= 8:
        raise ValueError("The engine exported too many or missing texture images.")
    textures = []
    for image in images:
        name = image.get("uri")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".png"):
            raise ValueError("Only adjacent PNG texture files may be embedded.")
        path = source.parent / name
        if path.is_symlink() or path.resolve().parent != source.parent.resolve():
            raise ValueError("The texture is outside this reconstruction.")
        if not 33 <= path.stat().st_size <= MAX_ASSET_BYTES - len(binary):
            raise ValueError("The texture exceeds its size limit.")
        data = path.read_bytes()
        if data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise ValueError("The texture is not a PNG image.")
        width, height = struct.unpack_from(">II", data, 16)
        pixels += width * height
        if not 1 <= width <= 4096 or not 1 <= height <= 4096 or pixels > MAX_TEXTURE_PIXELS:
            raise ValueError("The texture exceeds the phone viewer's pixel budget.")
        decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if decoded is None or decoded.shape[:2] != (height, width):
            raise ValueError("The texture is incomplete or cannot be decoded.")
        del decoded
        textures.append({"sha256": hashlib.sha256(data).hexdigest(), "width": width,
                         "height": height, "bytes": len(data)})
        image.clear()
        image.update(bufferView=len(document["bufferViews"]), mimeType="image/png")
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(binary),
                                        "byteLength": len(data)})
        binary.extend(data)
        binary.extend(b"\0" * (-len(binary) % 4))
    document["buffers"] = [{"byteLength": len(binary)}]
    document["extras"] = {"representation": "textured_mesh", "metric_scale": False,
                          "coordinate_frame": "local_relative", "hole_filling": False}
    stats = mesh_stats(document, bytes(binary))
    checksum = write_glb(destination, document, bytes(binary))
    return {**stats, "textures": textures, "artifact_sha256": checksum,
            "artifact_bytes": destination.stat().st_size}
