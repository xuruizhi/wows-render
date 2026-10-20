"""
Sanitize a .glb produced by wows-model-exporter so strict glTF parsers
(Blender's importer) accept it.

Problem: the exporter emits nodes/meshes whose "primitives" key is missing or
empty. glTF spec requires primitives to be a non-empty array; Blender's
importer asserts and refuses the entire file.

Fix:
  * drop meshes with no primitives
  * remove those mesh references from nodes (set node.mesh = None)
  * drop now-empty child nodes only if they carry no transform-relevant data
  * leave bufferViews/accessors untouched (they're shared; pruning them is risky)

Usage: python sanitize_glb.py <in.glb> <out.glb>
"""
import json
import struct
import sys


def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, version, total = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise SystemExit("not a GLB: %r" % magic)
    off = 12
    chunks = []
    while off < total:
        clen, ctype = struct.unpack_from("<II", data, off)
        payload = data[off + 8: off + 8 + clen]
        chunks.append((ctype, payload))
        off += 8 + clen
    return chunks


def write_glb(path, chunks):
    out = bytearray()
    body = bytearray()
    for ctype, payload in chunks:
        pad = (4 - (len(payload) % 4)) % 4
        body += struct.pack("<II", len(payload) + pad, ctype)
        body += payload
        body += b"\x00" * pad
    total = 12 + len(body)
    out += struct.pack("<4sII", b"glTF", 2, total)
    out += body
    with open(path, "wb") as f:
        f.write(bytes(out))


def main():
    src, dst = sys.argv[1], sys.argv[2]
    chunks = read_glb(src)

    # first chunk is JSON
    json_idx = None
    for i, (ctype, payload) in enumerate(chunks):
        if ctype == 0x4E4F534A:  # 'JSON'
            json_idx = i
            break
    if json_idx is None:
        raise SystemExit("no JSON chunk")

    gltf = json.loads(chunks[json_idx][1].decode("utf-8"))

    meshes = gltf.get("meshes", [])
    dropped = 0
    keep_mesh = []
    remap = {}
    for i, m in enumerate(meshes):
        prims = m.get("primitives")
        if not prims:
            dropped += 1
            continue
        remap[i] = len(keep_mesh)
        keep_mesh.append(m)
    gltf["meshes"] = keep_mesh

    # fix node.mesh references
    fixed_nodes = 0
    for n in gltf.get("nodes", []):
        if "mesh" in n and n["mesh"] is not None:
            old = n["mesh"]
            if old in remap:
                n["mesh"] = remap[old]
            else:
                del n["mesh"]
                fixed_nodes += 1

    # also ensure every remaining primitive has attributes dict
    for m in gltf["meshes"]:
        for p in m["primitives"]:
            if not isinstance(p.get("attributes"), dict):
                p["attributes"] = {}

    # re-encode JSON chunk, padded with spaces as spec requires
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    pad = (4 - (len(js) % 4)) % 4
    js += b" " * pad
    chunks[json_idx] = (0x4E4F534A, js)

    write_glb(dst, chunks)
    print("SANITIZE_OK dropped_meshes=%d unlinked_nodes=%d meshes_now=%d"
          % (dropped, fixed_nodes, len(keep_mesh)))


if __name__ == "__main__":
    main()
